#!/usr/bin/env python3
"""Harness clínico multi-llamada para experimentos auditables con Gemma4.

El harness no intenta capturar chain-of-thought privado. Registra prompts,
salidas estructuradas, resúmenes de decisión solicitados, validaciones,
reintentos y métricas de cada llamada.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from run_experiment import (
    build_excel_outputs,
    load_clinical_notes,
    load_model,
    run_inference_direct,
    select_notes_by_cohort,
    unload_model,
)

VALIDATION_POLICY = "evidence-id-exact-v2"


def build_evidence_index(note: str) -> tuple[str, dict[str, str]]:
    """Numera líneas para que el modelo seleccione y no redacte citas."""
    evidence_index: dict[str, str] = {}
    indexed_lines: list[str] = []
    for number, line in enumerate((line for line in note.splitlines() if line.strip()), 1):
        evidence_id = f"E{number:04d}"
        evidence_index[evidence_id] = line
        indexed_lines.append(f"[{evidence_id}] {line}")
    if not indexed_lines:
        evidence_index["E0001"] = note
        indexed_lines.append(f"[E0001] {note}")
    return "\n".join(indexed_lines), evidence_index


def add_evidence_id_schema(value: Any) -> Any:
    """Añade evidence_id junto a cada objeto que contiene evidencia."""
    if isinstance(value, dict):
        out = {key: add_evidence_id_schema(item) for key, item in value.items()}
        if "evidencia" in out:
            out["evidence_id"] = None
        return out
    if isinstance(value, list):
        return [add_evidence_id_schema(item) for item in value]
    return value


def canonicalize_evidence_ids(result: dict[str, Any], evidence_index: dict[str, str]) -> dict[str, Any]:
    """Copia al resultado el texto original asociado a cada ID válido."""
    if not isinstance(result, dict):
        return result
    output = json.loads(json.dumps(result, ensure_ascii=False))

    def visit(value: Any) -> None:
        if not isinstance(value, dict):
            return
        if "evidence_id" in value:
            ids = value.get("evidence_id")
            ids_list = [ids] if isinstance(ids, str) else ids
            if ids_list in (None, "", []):
                value["evidencia"] = None
            elif isinstance(ids_list, list) and all(item in evidence_index for item in ids_list):
                value["evidencia"] = "\n".join(evidence_index[item] for item in ids_list)
            else:
                value["evidencia"] = None
        for nested in value.values():
            if isinstance(nested, dict):
                visit(nested)

    visit(output)
    return output


def project_domain_output(result: Any, fields: dict[str, Any]) -> tuple[Any, list[str]]:
    """Conserva solo el dominio solicitado y registra claves extra."""
    if not isinstance(result, dict):
        return result, []
    unexpected = sorted(set(result) - set(fields) - {"_audit"})
    projected = {key: result[key] for key in fields if key in result}
    if "_audit" in result:
        projected["_audit"] = result["_audit"]
    return projected, unexpected


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_skill_bundle(skills_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    common = (skills_dir / "common.md").read_text(encoding="utf-8")
    skills = []
    for path in sorted(skills_dir.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not {"name", "instructions", "fields"} <= data.keys():
            raise ValueError(f"Skill clínica inválida: {path}")
        data["_path"] = str(path)
        skills.append(data)
    if not skills:
        raise ValueError(f"No se encontraron skills en {skills_dir}")
    return common, skills


def render_skill_prompt(common: str, skill: dict[str, Any]) -> str:
    template = json.dumps(add_evidence_id_schema(skill["fields"]), ensure_ascii=False, indent=2)
    return f"""\
{common}

# Skill activa: {skill['name']}

{skill['instructions'].strip()}

Devuelve exactamente las claves de este dominio, más `_audit`.
Cada objeto que contiene `evidencia` contiene también `evidence_id`. Devuelve
un ID de la epicrisis indexada, o una lista de IDs consecutivos. No redactes
el texto de evidencia: el harness lo copiará literalmente desde la nota.

{template}

`_audit` debe tener esta forma:
{{
  "resumen": "explicación clínica breve, sin razonamiento paso a paso",
  "incertidumbres": ["campo: motivo breve"],
  "reglas_aplicadas": ["regla breve"]
}}
"""


def render_index_prompt(common: str) -> str:
    return f"""\
{common}

# Etapa: mapa clínico

Antes de extraer variables, crea un mapa breve de la epicrisis. Identifica
fragmentos literales relevantes por dominio y su temporalidad. No decidas aún
los 48 campos.

Devuelve (usa `evidence_id` para las citas):
{{
  "clinical_map": {{
    "antecedentes": [{{"evidencia": "...", "temporalidad": "previo|incierto"}}],
    "episodio_uci": [{{"evidencia": "...", "temporalidad": "actual|incierto"}}],
    "infecciones": [{{"evidencia": "...", "temporalidad": "previo|ingreso|intrahospitalario|incierto"}}],
    "fallas_organicas": [{{"evidencia": "...", "temporalidad": "actual|incierto"}}],
    "diagnosticos": [{{"evidencia": "...", "temporalidad": "ingreso|egreso|incierto"}}]
  }},
  "_audit": {{
    "resumen": "resumen breve de organización",
    "incertidumbres": [],
    "reglas_aplicadas": ["separación temporal"]
  }}
}}
"""


def validate_value(
    field: str,
    value: Any,
    template: Any,
    note: str,
    evidence_index: dict[str, str] | None = None,
) -> list[str]:
    errors: list[str] = []
    if isinstance(template, dict):
        if not isinstance(value, dict):
            return [f"{field}: se esperaba objeto, se recibió {type(value).__name__}"]
        for key, nested_template in template.items():
            if key not in value:
                errors.append(f"{field}.{key}: clave faltante")
                continue
            errors.extend(
                validate_value(
                    f"{field}.{key}", value[key], nested_template, note,
                    evidence_index
                )
            )
        if "valor" in template:
            state = value.get("valor")
            evidence_id = value.get("evidence_id")
            evidence = value.get("evidencia")
            if state not in (True, False, None):
                errors.append(f"{field}.valor: debe ser true, false o null")
            if state in (True, False):
                if evidence_index is not None and evidence_id in (None, "", []):
                    errors.append(
                        f"{field}.evidence_id: obligatorio cuando valor={str(state).lower()}"
                    )
                if not isinstance(evidence, str) or not evidence:
                    errors.append(
                        f"{field}.evidencia: obligatoria cuando valor={str(state).lower()}"
                    )
                elif evidence not in note:
                    errors.append(
                        f"{field}.evidencia: debe ser un tramo textual exacto "
                        "de la epicrisis (mismos caracteres)"
                    )
            elif evidence not in (None, ""):
                if not isinstance(evidence, str) or evidence not in note:
                    errors.append(
                        f"{field}.evidencia: con valor=null debe ser null o "
                        "un tramo textual exacto de la epicrisis"
                    )
            if evidence_index is not None and evidence not in (None, "") and evidence_id in (None, "", []):
                errors.append(f"{field}.evidence_id: toda evidencia debe provenir de un ID")
            if evidence_index is not None and evidence_id not in (None, "", []):
                ids = [evidence_id] if isinstance(evidence_id, str) else evidence_id
                if not isinstance(ids, list) or not ids or not all(item in evidence_index for item in ids):
                    errors.append(f"{field}.evidence_id: contiene un ID inexistente o inválido")
        return errors

    if template is None and value is not None and isinstance(value, (dict, list)):
        errors.append(f"{field}: se esperaba dato simple o null")
    return errors


def validate_domain(
    result: dict[str, Any],
    fields: dict[str, Any],
    note: str,
    evidence_index: dict[str, str] | None = None,
) -> list[str]:
    if not isinstance(result, dict):
        return ["respuesta: se esperaba objeto JSON"]
    errors: list[str] = []
    for field, template in fields.items():
        if field not in result:
            errors.append(f"{field}: campo faltante")
            continue
        errors.extend(validate_value(field, result[field], template, note, evidence_index))
    unexpected = sorted(set(result) - set(fields) - {"_audit"})
    if unexpected:
        errors.append(f"campos inesperados: {', '.join(unexpected)}")
    if "hfav" in fields and isinstance(result.get("hfav"), dict):
        hfav = result["hfav"]
        state = hfav.get("valor")
        evidence = hfav.get("evidencia")
        evidence_folded = evidence.casefold() if isinstance(evidence, str) else ""
        explicit_hfav = (
            re.search(r"\bhfav\b", evidence_folded) is not None
            or "hemofiltración de alto volumen" in evidence_folded
            or "hemofiltracion de alto volumen" in evidence_folded
            or "alto volumen" in evidence_folded
        )
        if state is True and not explicit_hfav:
            errors.append(
                "hfav.valor: true exige que la cita exacta mencione HFAV "
                "o hemofiltración de alto volumen"
            )
        if state is False:
            explicit_negation = re.search(
                r"\b(?:no|sin|niega|negada|descarta|descartada)\b",
                evidence_folded,
            )
            if not explicit_hfav or explicit_negation is None:
                errors.append(
                    "hfav.valor: false exige una cita exacta que niegue "
                    "explícitamente HFAV o alto volumen"
                )
    return errors


def validate_selected_fields(
    result: Any,
    fields: dict[str, Any],
    note: str,
    evidence_index: dict[str, str],
) -> list[str]:
    """Valida únicamente los campos solicitados en un reintento parcial."""
    if not isinstance(result, dict):
        return ["respuesta: se esperaba objeto JSON"]
    errors: list[str] = []
    for field, template in fields.items():
        if field not in result:
            errors.append(f"{field}: campo faltante")
        else:
            errors.extend(validate_value(field, result[field], template, note, evidence_index))
    return errors


def collect_evidence_spans(
    result: dict[str, Any],
    note: str,
) -> dict[str, dict[str, Any]]:
    """Registra offsets verificables para cada cita exacta de la respuesta."""
    spans: dict[str, dict[str, Any]] = {}

    def visit(value: Any, path: str) -> None:
        if not isinstance(value, dict):
            return
        evidence = value.get("evidencia")
        if isinstance(evidence, str) and evidence:
            start = note.find(evidence)
            spans[path] = {
                "exact_match": start >= 0,
                "start_char": start if start >= 0 else None,
                "end_char": start + len(evidence) if start >= 0 else None,
                "length_chars": len(evidence),
                "sha256": hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
            }
        for key, nested in value.items():
            if key != "evidencia" and isinstance(nested, dict):
                visit(nested, f"{path}.{key}" if path else key)

    for field, value in result.items():
        if not field.startswith("_"):
            visit(value, field)
    return spans


def compact_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Conserva métricas completas salvo la serie GPU, que ya puede ser masiva."""
    out = dict(meta)
    samples = out.pop("gpu_timeseries", [])
    out["gpu_samples_n"] = len(samples)
    return out


def token_budget_for_stage(stage: str, settings: dict[str, Any]) -> int:
    """Resuelve el límite de emergencia específico para cada llamada."""
    budgets = settings.get("token_budgets", {})
    if not isinstance(budgets, dict):
        raise ValueError("harness.token_budgets debe ser un objeto YAML")
    if stage == "clinical_map":
        key = "clinical_map"
    elif ":attempt:" in stage and not stage.endswith(":attempt:1"):
        key = "retry"
    elif stage.startswith("skill:"):
        key = stage.split(":", 2)[1]
    else:
        key = "default"
    return int(
        budgets.get(
            key,
            budgets.get("default", settings.get("max_new_tokens_per_call", 1200)),
        )
    )


def call_model(
    *,
    stage: str,
    prompt: str,
    note: str,
    model: Any,
    tokenizer: Any,
    family: str,
    settings: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = utc_now()
    token_budget = token_budget_for_stage(stage, settings)
    output, _, meta = run_inference_direct(
        note,
        model,
        tokenizer,
        family,
        system_prompt=prompt,
        max_new_tokens=token_budget,
        temperature=float(settings.get("temperature", 0.0)),
        top_p=float(settings.get("top_p", 1.0)),
        enable_thinking=False,
        thinking_budget=None,
        stop_on_complete_json=bool(settings.get("stop_on_complete_json", True)),
        record_raw_response=bool(settings.get("record_raw_responses", True)),
    )
    trace = {
        "stage": stage,
        "started_at": started,
        "finished_at": utc_now(),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt": prompt if settings.get("record_prompts", True) else None,
        "max_new_tokens": token_budget,
        "output": output,
        "inference": compact_meta(meta),
    }
    return output, trace


def process_patient(
    *,
    patient_id: str,
    note: str,
    common: str,
    skills: list[dict[str, Any]],
    model: Any,
    tokenizer: Any,
    family: str,
    settings: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    trace: dict[str, Any] = {
        "trace_version": str(settings.get("version", "harness-v1")),
        "patient_id": patient_id,
        "started_at": utc_now(),
        "note_sha256": hashlib.sha256(note.encode("utf-8")).hexdigest(),
        "calls": [],
    }

    indexed_note, evidence_index = build_evidence_index(note)
    clinical_map, index_trace = call_model(
        stage="clinical_map",
        prompt=render_index_prompt(common),
        note=indexed_note,
        model=model,
        tokenizer=tokenizer,
        family=family,
        settings=settings,
    )
    trace["calls"].append(index_trace)

    merged: dict[str, Any] = {}
    final_errors_by_skill: dict[str, list[str]] = {}
    max_retries = int(settings.get("max_retries", 1))
    map_context = json.dumps(clinical_map, ensure_ascii=False, indent=2)

    for skill in skills:
        base_prompt = render_skill_prompt(common, skill)
        prompt = (
            f"{base_prompt}\n\n# Mapa clínico de la llamada anterior\n{map_context}\n"
            "Contrasta siempre el mapa contra la epicrisis original."
        )
        output, call_trace = call_model(
            stage=f"skill:{skill['name']}:attempt:1",
            prompt=prompt,
            note=indexed_note,
            model=model,
            tokenizer=tokenizer,
            family=family,
            settings=settings,
        )
        output, unexpected = project_domain_output(output, skill["fields"])
        call_trace["unexpected_fields_projected"] = unexpected
        output = canonicalize_evidence_ids(output, evidence_index)
        errors = validate_domain(output, skill["fields"], note, evidence_index)
        call_trace["validation_errors"] = errors
        call_trace["evidence_spans"] = collect_evidence_spans(output, note)
        trace["calls"].append(call_trace)

        attempt = 1
        while errors and attempt <= max_retries:
            attempt += 1
            failed_fields = sorted({error.split(".", 1)[0] for error in errors if error})
            failed_fields = [field for field in failed_fields if field in skill["fields"]]
            retry_fields = {field: skill["fields"][field] for field in failed_fields}
            retry_schema = json.dumps(
                add_evidence_id_schema(retry_fields), ensure_ascii=False, indent=2
            )
            retry_prompt = f"""\
{common}

# Corrección selectiva del dominio {skill['name']}

La epicrisis está indexada. Devuelve solamente estos campos fallidos y
`_audit`; no vuelvas a generar campos que ya estaban válidos:

{retry_schema}

Resultado previo de esos campos:
{json.dumps({field: output.get(field) for field in failed_fields}, ensure_ascii=False, indent=2)}

Errores determinísticos:
{json.dumps(errors, ensure_ascii=False, indent=2)}

Corrige únicamente los campos listados. Para evidencia devuelve `evidence_id`
de la nota; no escribas una cita. No inventes evidencia.
"""
            output, retry_trace = call_model(
                stage=f"skill:{skill['name']}:attempt:{attempt}",
                prompt=retry_prompt,
                note=indexed_note,
                model=model,
                tokenizer=tokenizer,
                family=family,
                settings=settings,
            )
            retry_output, unexpected = project_domain_output(output, retry_fields)
            retry_trace["unexpected_fields_projected"] = unexpected
            retry_output = canonicalize_evidence_ids(retry_output, evidence_index)
            retry_errors = validate_selected_fields(
                retry_output, retry_fields, note, evidence_index
            )
            if not retry_errors:
                for field in failed_fields:
                    output[field] = retry_output[field]
            errors = validate_domain(output, skill["fields"], note, evidence_index)
            retry_trace["repaired_fields"] = failed_fields if not retry_errors else []
            retry_trace["partial_validation_errors"] = retry_errors
            retry_trace["validation_errors"] = errors
            retry_trace["evidence_spans"] = collect_evidence_spans(output, note)
            trace["calls"].append(retry_trace)

        for field in skill["fields"]:
            merged[field] = output.get(field)
        final_errors_by_skill[skill["name"]] = errors

    trace["finished_at"] = utc_now()
    trace["calls_n"] = len(trace["calls"])
    trace["validation_errors_final"] = final_errors_by_skill
    trace["valid_final"] = not any(final_errors_by_skill.values())
    trace["validation_policy"] = VALIDATION_POLICY
    trace["evidence_index"] = evidence_index
    trace["evidence_spans_final"] = collect_evidence_spans(merged, note)
    return merged, trace


def run_harness(config_path: Path, requested_model: str | None = None) -> None:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    script_dir = config_path.parents[2]
    exp_dir = config_path.parent
    model_configs = cfg["models"]
    if requested_model:
        matches = [m for m in model_configs if m["name"] == requested_model]
        if not matches:
            raise ValueError(
                f"Modelo {requested_model!r} no está definido en {config_path}"
            )
        model_cfg = matches[0]
    elif len(model_configs) == 1:
        model_cfg = model_configs[0]
    else:
        raise ValueError("--model_name es obligatorio cuando config.yml tiene varios modelos")
    model_name = model_cfg["name"]

    model_dir = exp_dir / model_name
    jsons_dir = model_dir / "jsons"
    traces_dir = model_dir / "traces"
    invalid_dir = model_dir / "invalid_jsons"
    jsons_dir.mkdir(parents=True, exist_ok=True)
    traces_dir.mkdir(parents=True, exist_ok=True)
    invalid_dir.mkdir(parents=True, exist_ok=True)

    excel_path = str(cfg["experiment"].get("excel_path", "")).strip()
    if not excel_path:
        candidates = sorted((script_dir / "excel_resumen").glob("*.xlsx"))
        if not candidates:
            raise FileNotFoundError("No hay Excel en excel_resumen/")
        excel_path = str(candidates[0])

    skills_dir = script_dir / cfg["harness"]["skills_dir"]
    common, skills = load_skill_bundle(skills_dir)
    n_epicrisis = int(cfg["experiment"].get("n_epicrisis", 0))
    cohort_ids_rel = str(cfg["experiment"].get("cohort_ids_file", "")).strip()
    cohort_ids_file = str(script_dir / cohort_ids_rel) if cohort_ids_rel else None
    notes, _, df_original = load_clinical_notes(
        excel_path, 0 if cohort_ids_file else n_epicrisis
    )
    notes = select_notes_by_cohort(notes, cohort_ids_file)
    if n_epicrisis > 0:
        notes = notes[:n_epicrisis]
    notes_by_id = {note["paciente_id"]: note["nota_clinica"] for note in notes}

    def result_is_valid(result_path: Path, patient_id: str) -> bool:
        trace_path = traces_dir / f"{patient_id}__{model_name}_trace.json"
        note = notes_by_id.get(patient_id)
        if note is None:
            return False
        try:
            result_data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        _, evidence_index = build_evidence_index(note)
        result_data = canonicalize_evidence_ids(result_data, evidence_index)
        errors_by_skill = {
            skill["name"]: validate_domain(result_data, skill["fields"], note, evidence_index)
            for skill in skills
        }
        valid = not any(errors_by_skill.values())
        # Migra la traza anterior a la política exacta sin volver a llamar al
        # modelo cuando el JSON existente ya cumple todas las invariantes.
        try:
            trace_data = (
                json.loads(trace_path.read_text(encoding="utf-8"))
                if trace_path.exists() else {}
            )
        except (OSError, json.JSONDecodeError):
            trace_data = {}
        trace_data["validation_policy"] = VALIDATION_POLICY
        trace_data["validation_errors_final"] = errors_by_skill
        trace_data["valid_final"] = valid
        trace_data["evidence_spans_final"] = collect_evidence_spans(result_data, note)
        trace_path.write_text(
            json.dumps(trace_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return valid

    existing: set[str] = set()
    for result_path in jsons_dir.glob(f"*__{model_name}.json"):
        if "_meta" in result_path.name:
            continue
        patient_id = result_path.stem.split("__")[0]
        if result_is_valid(result_path, patient_id):
            existing.add(patient_id)
        else:
            archived = invalid_dir / result_path.name
            if archived.exists():
                archived = invalid_dir / (
                    f"{patient_id}__{model_name}_previous_{int(datetime.now().timestamp())}.json"
                )
            result_path.replace(archived)
            print(
                f"  [INVALID] {patient_id}: resultado previo archivado en "
                f"{archived.name}",
                flush=True,
            )
    pending = [note for note in notes if note["paciente_id"] not in existing]
    print(f"  [HARNESS] {len(existing)} completos → {len(pending)} pendientes")
    if not pending:
        build_excel_outputs(model_name, model_dir, jsons_dir, df_original)
        return

    model, tokenizer, family = load_model(model_name)
    settings = {
        **cfg.get("harness", {}),
        **model_cfg.get("hyperparams", {}),
    }
    try:
        for index, note_data in enumerate(pending, 1):
            patient_id = note_data["paciente_id"]
            note = note_data["nota_clinica"]
            print(f"\n  [HARNESS {index}/{len(pending)}] {patient_id}", flush=True)
            if len(note.strip()) < 50:
                print("    Nota vacía o demasiado corta; omitida", flush=True)
                continue
            try:
                result, trace = process_patient(
                    patient_id=patient_id,
                    note=note,
                    common=common,
                    skills=skills,
                    model=model,
                    tokenizer=tokenizer,
                    family=family,
                    settings=settings,
                )
                trace_path = traces_dir / f"{patient_id}__{model_name}_trace.json"
                trace_path.write_text(
                    json.dumps(trace, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                if trace["valid_final"]:
                    (jsons_dir / f"{patient_id}__{model_name}.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    print(
                        f"    OK VÁLIDO: {trace['calls_n']} llamadas registradas",
                        flush=True,
                    )
                else:
                    (invalid_dir / f"{patient_id}__{model_name}.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    n_errors = sum(
                        len(errors)
                        for errors in trace["validation_errors_final"].values()
                    )
                    print(
                        f"    INVÁLIDO: {n_errors} error(es); no se publica en jsons/",
                        flush=True,
                    )
            except Exception as exc:
                print(f"    ERROR paciente {patient_id}: {exc}", flush=True)
    finally:
        unload_model(model, model_name)

    build_excel_outputs(model_name, model_dir, jsons_dir, df_original)


def launch(config_path: Path) -> None:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    script_dir = config_path.parents[2]
    exp_dir = config_path.parent
    slurm = cfg.get("slurm", {})
    conda_env = os.environ.get("CONDA_ENV", os.environ.get("CONDA_DEFAULT_ENV", "newLLMfabian2"))
    submitted: list[tuple[str, str]] = []
    for model_cfg in cfg["models"]:
        model_name = model_cfg["name"]
        model_dir = exp_dir / model_name
        logs_dir = model_dir / "slurm_logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        job_path = model_dir / "job_harness.sh"

        n_epicrisis = int(cfg["experiment"].get("n_epicrisis", 0))
        jsons_dir = model_dir / "jsons"
        done = (
            len([p for p in jsons_dir.glob(f"*__{model_name}.json")
                 if "_meta" not in p.name])
            if jsons_dir.exists() else 0
        )
        if n_epicrisis > 0 and done >= n_epicrisis:
            print(f"  [SKIP] {model_name}: {done}/{n_epicrisis} completos")
            continue

        job = f"""#!/bin/bash
# Job harness clínico auto-generado
#SBATCH --nodelist={slurm.get('nodelist', 'ih-condor')}
#SBATCH --job-name=harness_{exp_dir.name[:10]}_{model_name}
#SBATCH -t {slurm.get('time', '03:00:00')}
#SBATCH -p {slurm.get('partition', 'batch')}
#SBATCH -q {slurm.get('qos', 'batch')}
#SBATCH --gpus={slurm.get('gpus', 2)}
#SBATCH --cpus-per-task={slurm.get('cpus_per_task', 10)}
#SBATCH --mem={slurm.get('mem', '32G')}
#SBATCH --output={logs_dir}/slurm_%j.out
#SBATCH --error={logs_dir}/slurm_%j.err

module load micromamba
micromamba activate {conda_env}
set -eo pipefail

python3 {script_dir}/extraction/run_harness_experiment.py \\
  --config {config_path} \\
  --model_name {model_name}
"""
        job_path.write_text(job, encoding="utf-8")
        job_path.chmod(0o755)
        result = subprocess.run(["sbatch", str(job_path)], capture_output=True, text=True)
        if result.returncode:
            print(f"  [ERROR] {model_name}: {result.stderr.strip()}")
            continue
        job_id = result.stdout.strip().split()[-1]
        submitted.append((model_name, job_id))
        print(f"  [OK] {model_name} → job {job_id}")
    print(f"\n{submitted and len(submitted) or 0} job(s) enviados")


def main() -> None:
    parser = argparse.ArgumentParser(description="Harness clínico multi-llamada")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--model_name", default=None)
    parser.add_argument("--launch", action="store_true")
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    if args.launch:
        launch(config_path)
    else:
        run_harness(config_path, requested_model=args.model_name)


if __name__ == "__main__":
    main()
