#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_experiment.py — Orquestador de experimentos LLM.

Modos de uso:
  1. Launcher (desde run_experiment.sh):
       python3 run_experiment.py --launch \\
           --experiment InitialTests-full-GLQ \\
           --script_dir /path/to/extraction-condor

  2. Extracción de un solo modelo (llamado por sbatch):
       python3 run_experiment.py \\
           --experiment_dir /path/to/experiments/InitialTests-full-GLQ \\
           --model_name Gemma4 \\
           --fields_file /path/to/llm-prompts/fields/with-evidence.txt \\
           --context_file /path/to/llm-prompts/context/full.txt \\
           --excel_path /path/to/epicrisis.xlsx \\
           --n_epicrisis 10 \\
           --temperature 0 --max_new_tokens 8000 --top_p 1.0
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

try:
    import torch
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
        StoppingCriteria,
        StoppingCriteriaList,
    )
    _HAS_TORCH = True
except ImportError:
    torch = None  # type: ignore[assignment]
    AutoModelForCausalLM = AutoTokenizer = BitsAndBytesConfig = None  # type: ignore[assignment,misc]
    StoppingCriteria = object  # type: ignore[assignment,misc]
    StoppingCriteriaList = None  # type: ignore[assignment,misc]
    _HAS_TORCH = False


# ─────────────────────────────────────────────
# RUTAS DE MODELOS EN IH-CONDOR
# ─────────────────────────────────────────────

MODELS = {
    "Gemma4":    "/path/to/models/google/gemma-4-31B-it",
    "MedGemma3": "/path/to/models/google/medgemma-27b-it",
    "Llama-70B": "/path/to/models/meta-llama/Llama-3.3-70B-Instruct",
    "Qwen":      "/path/to/models/Qwen/Qwen3.6-35B-A3B",
}

FAMILY_GEMMA = "gemma"
FAMILY_LLAMA = "llama"
FAMILY_QWEN  = "qwen"
FAMILY_OTHER = "other"

GPU_SAMPLE_INTERVAL_S = 0.5
VRAM_WARN_THRESHOLD   = 0.80
VRAM_ABORT_THRESHOLD  = 0.93
VRAM_4BIT_THRESHOLD   = 60.0

EXCEL_ID_COL   = "ID Paciente Anónimo"
EXCEL_TEXT_COL = "Resumen Clínico"

USER_INSTRUCTIONS_PREFIX = "Analiza la siguiente nota clínica y devuelve el JSON estructurado.\n\nNOTA CLÍNICA COMPLETA:\n"
USER_INSTRUCTIONS_SUFFIX = "\n\nResponde SOLO con el JSON válido."


# ─────────────────────────────────────────────
# PROMPT DESDE ARCHIVOS
# ─────────────────────────────────────────────

def load_system_prompt(fields_file: str, context_file: str) -> str:
    context = Path(context_file).read_text(encoding="utf-8").strip()
    fields  = Path(fields_file).read_text(encoding="utf-8").strip()
    return context + "\n\n" + fields


# ─────────────────────────────────────────────
# FAMILIA DE MODELOS
# ─────────────────────────────────────────────

def get_model_family(model_name: str) -> str:
    name = model_name.lower()
    if "gemma" in name:
        return FAMILY_GEMMA
    if "llama" in name:
        return FAMILY_LLAMA
    if "qwen" in name:
        return FAMILY_QWEN
    return FAMILY_OTHER


# ─────────────────────────────────────────────
# CONSTRUCCIÓN DE MENSAJES
# ─────────────────────────────────────────────

def build_messages(
    system_prompt: str,
    user_content: str,
    model_family: str,
) -> List[Dict[str, str]]:
    if model_family == FAMILY_GEMMA:
        return [{"role": "user", "content": system_prompt + "\n\n" + user_content}]
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_content},
    ]


# ─────────────────────────────────────────────
# TERMINATORS
# ─────────────────────────────────────────────

def get_terminators(tokenizer, model_family: str, model=None) -> List[int]:
    """Obtiene EOS declarados por tokenizer, modelo y plantilla de chat.

    Gemma y Llama pueden terminar un turno con un token distinto del EOS
    general. Sólo incorporamos tokens existentes en el vocabulario para no
    convertir accidentalmente un token desconocido en terminador.
    """
    terminators: List[int] = []

    def add(candidate) -> None:
        candidates = candidate if isinstance(candidate, (list, tuple, set)) else [candidate]
        for token_id in candidates:
            if isinstance(token_id, int) and token_id >= 0 and token_id not in terminators:
                terminators.append(token_id)

    add(tokenizer.eos_token_id)
    if model is not None:
        add(getattr(getattr(model, "generation_config", None), "eos_token_id", None))
        add(getattr(getattr(model, "config", None), "eos_token_id", None))

    family_tokens = {
        FAMILY_GEMMA: ("<end_of_turn>", "<eos>"),
        FAMILY_LLAMA: ("<|eot_id|>",),
        FAMILY_QWEN: ("<|im_end|>",),
    }
    for token in family_tokens.get(model_family, ()):
        token_id = tokenizer.convert_tokens_to_ids(token)
        if token_id is not None and token_id != tokenizer.unk_token_id:
            add(token_id)

    if not terminators:
        raise ValueError("Tokenizer/modelo no declaran ningún token EOS válido")
    return terminators


def find_complete_json_end(text: str) -> Optional[int]:
    """Retorna el índice posterior al primer objeto JSON externo completo.

    El contador ignora llaves dentro de strings y respeta escapes. Permite
    texto o fences antes del primer ``{`` porque algunos modelos los agregan
    aun cuando el prompt solicita JSON puro.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


class JsonObjectStoppingCriteria(StoppingCriteria):
    """Detiene ``generate`` cuando la respuesta contiene un JSON completo."""

    def __init__(self, tokenizer, input_length: int):
        self.tokenizer = tokenizer
        self.input_length = input_length
        self.completed = False
        self.json_end_character: Optional[int] = None

    def __call__(self, input_ids, scores, **kwargs):
        # El harness procesa una epicrisis por vez; se inspecciona la primera
        # secuencia del batch. Decodificar aquí cuesta poco frente a la
        # inferencia de Gemma 31B y evita cientos de tokens desperdiciados.
        generated = input_ids[0][self.input_length:]
        text = self.tokenizer.decode(generated, skip_special_tokens=True)
        end = find_complete_json_end(text)
        if end is not None:
            self.completed = True
            self.json_end_character = end
            return True
        return False


# ─────────────────────────────────────────────
# CHAT TEMPLATE
# ─────────────────────────────────────────────

def apply_template_for_family(
    tokenizer,
    messages,
    model_family: str,
    enable_thinking: bool = False,
    thinking_budget: Optional[int] = 1024,
):
    kwargs = {
        "return_tensors":        "pt",
        "add_generation_prompt": True,
        "return_dict":           True,
    }
    if model_family in (FAMILY_QWEN, FAMILY_GEMMA):
        kwargs["enable_thinking"] = enable_thinking
        if enable_thinking and thinking_budget is not None:
            kwargs["thinking_budget"] = thinking_budget

    try:
        return tokenizer.apply_chat_template(messages, **kwargs)
    except TypeError as exc:
        unsupported = [k for k in ("enable_thinking", "thinking_budget") if k in str(exc)]
        if unsupported:
            for k in unsupported:
                kwargs.pop(k, None)
            print(f"  AVISO: {unsupported} no soportado — se ignora", flush=True)
            return tokenizer.apply_chat_template(messages, **kwargs)
        raise


# ─────────────────────────────────────────────
# VRAM
# ─────────────────────────────────────────────

def get_vram_stats(device: int = 0) -> dict:
    if not torch.cuda.is_available():
        return {"error": "CUDA no disponible"}
    total     = torch.cuda.get_device_properties(device).total_memory
    reserved  = torch.cuda.memory_reserved(device)
    allocated = torch.cuda.memory_allocated(device)
    return {
        "used_gb":      round(reserved  / 1024**3, 2),
        "total_gb":     round(total     / 1024**3, 2),
        "free_gb":      round((total - reserved) / 1024**3, 2),
        "usage_pct":    round(reserved  / total * 100, 1),
        "allocated_gb": round(allocated / 1024**3, 2),
    }


def print_vram(label: str = "", device: int = 0) -> dict:
    stats = get_vram_stats(device)
    if "error" in stats:
        print(f"  [VRAM] {stats['error']}")
        return stats
    pct    = stats["usage_pct"]
    bar    = "█" * int(pct / 100 * 30) + "░" * (30 - int(pct / 100 * 30))
    status = ("CRITICO" if pct >= VRAM_ABORT_THRESHOLD * 100
              else "ALERTA" if pct >= VRAM_WARN_THRESHOLD * 100
              else "OK")
    prefix = f"  [{label}] " if label else "  "
    print(f"{prefix}VRAM {status} | [{bar}] {pct:.1f}% | "
          f"{stats['used_gb']:.2f}/{stats['total_gb']:.2f} GiB | "
          f"Libre: {stats['free_gb']:.2f} GiB")
    if pct >= VRAM_ABORT_THRESHOLD * 100:
        raise RuntimeError("[VRAM] Uso crítico — ejecución detenida.")
    if pct >= VRAM_WARN_THRESHOLD * 100:
        warnings.warn(f"[VRAM] Uso al {pct:.1f}%", ResourceWarning, stacklevel=2)
    return stats


def monitor_gpu_during_inference(
    stop_event: threading.Event,
    samples: list,
    interval_s: float = GPU_SAMPLE_INTERVAL_S,
    n_devices: Optional[int] = None,
) -> None:
    if not torch.cuda.is_available():
        return
    n_devices = n_devices or torch.cuda.device_count()
    while not stop_event.is_set():
        ts   = datetime.now(timezone.utc).isoformat()
        gpus = []
        for dev in range(n_devices):
            stats = get_vram_stats(dev)
            if "error" not in stats:
                gpus.append({"device": dev, "used_gb": stats["used_gb"], "usage_pct": stats["usage_pct"]})
        samples.append({"timestamp": ts, "gpus": gpus})
        stop_event.wait(timeout=interval_s)


def summarize_gpu_samples(samples: list) -> dict:
    if not samples:
        return {}
    n_devices = len(samples[0].get("gpus", []))
    summary   = {}
    for dev in range(n_devices):
        used_series = [s["gpus"][dev]["used_gb"]   for s in samples if dev < len(s["gpus"])]
        pct_series  = [s["gpus"][dev]["usage_pct"] for s in samples if dev < len(s["gpus"])]
        if not used_series:
            continue
        summary[f"gpu{dev}"] = {
            "used_gb_min":    round(min(used_series),  2),
            "used_gb_max":    round(max(used_series),  2),
            "used_gb_mean":   round(sum(used_series) / len(used_series), 2),
            "usage_pct_min":  round(min(pct_series),   1),
            "usage_pct_max":  round(max(pct_series),   1),
            "usage_pct_mean": round(sum(pct_series)  / len(pct_series),  1),
        }
    return summary


# ─────────────────────────────────────────────
# TOKENS
# ─────────────────────────────────────────────

def count_tokens(tokenizer, text: str) -> int:
    return len(tokenizer.encode(text, add_special_tokens=False))


def separate_thinking(raw_output: str) -> Tuple[Optional[str], str]:
    match = re.search(r"<think>(.*?)</think>(.*)", raw_output, re.DOTALL)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return None, raw_output.strip()


# ─────────────────────────────────────────────
# CARGA DEL MODELO
# ─────────────────────────────────────────────

def load_model(model_name: str) -> tuple:
    if not _HAS_TORCH:
        raise RuntimeError("torch/transformers no instalados — necesarios para carga de modelos.")
    path = MODELS.get(model_name)
    if not path:
        raise KeyError(f"Modelo '{model_name}' no configurado. Disponibles: {list(MODELS.keys())}")

    family = get_model_family(model_name)
    print(f"\n{'='*55}\n  Modelo  : {model_name}\n  Familia : {family}\n  Path    : {path}\n{'='*55}")
    print_vram("Antes de cargar")

    if torch.cuda.is_available():
        total_vram_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
        use_4bit = total_vram_gb < VRAM_4BIT_THRESHOLD
        print(f"  VRAM total: {total_vram_gb:.1f} GB → {'4-bit NF4' if use_4bit else 'bfloat16'}")
    else:
        use_4bit = True

    tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token_id = tokenizer.eos_token_id

    load_kwargs: dict = {"device_map": "auto", "local_files_only": True}
    if use_4bit:
        load_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
        )
    else:
        load_kwargs["torch_dtype"] = torch.bfloat16

    t0    = time.time()
    model = AutoModelForCausalLM.from_pretrained(path, **load_kwargs)
    print(f"  Cargado en {time.time() - t0:.1f}s")
    print(f"  device_map: {getattr(model, 'hf_device_map', 'no disponible')}")
    print(
        "  tokens especiales: "
        f"eos={tokenizer.eos_token!r}/{tokenizer.eos_token_id}, "
        f"pad={tokenizer.pad_token!r}/{tokenizer.pad_token_id}"
    )
    print_vram("Después de cargar")
    return model, tokenizer, family


def unload_model(model, model_name: str = "") -> None:
    del model
    try:
        torch.cuda.empty_cache()
        print_vram(
            f"Después de descargar {model_name}" if model_name
            else "Después de descargar"
        )
    except RuntimeError as exc:
        # Un fallo CUDA asíncrono al finalizar no debe invalidar resultados que
        # ya fueron escritos y verificados paciente a paciente.
        print(f"  [AVISO] No fue posible vaciar caché CUDA al finalizar: {exc}")


# ─────────────────────────────────────────────
# CARGA DE EXCEL
# ─────────────────────────────────────────────

def load_clinical_notes(excel_path: str, n_epicrisis: int = 0) -> Tuple[List[Dict], Path, pd.DataFrame]:
    path = Path(excel_path)
    if not path.exists():
        raise FileNotFoundError(f"Excel no encontrado: {path}")

    df = pd.read_excel(path, dtype=str)
    missing = [c for c in (EXCEL_ID_COL, EXCEL_TEXT_COL) if c not in df.columns]
    if missing:
        raise ValueError(f"Columnas faltantes en Excel: {missing}")

    notes = []
    for idx, row in df.iterrows():
        notes.append({
            "paciente_id":  str(row[EXCEL_ID_COL]).strip(),
            "nota_clinica": str(row[EXCEL_TEXT_COL]).strip() if pd.notna(row[EXCEL_TEXT_COL]) else "",
            "row_index":    idx,
        })

    if n_epicrisis > 0:
        notes = notes[:n_epicrisis]

    print(f"  {len(notes)} notas cargadas desde {path.name}")
    return notes, path, df


def select_notes_by_cohort(
    notes: List[Dict[str, Any]],
    cohort_ids_file: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Selecciona y ordena notas mediante un manifiesto reproducible de IDs."""
    if not cohort_ids_file:
        return notes
    cohort_path = Path(cohort_ids_file)
    if not cohort_path.exists():
        raise FileNotFoundError(f"Manifiesto de cohorte no encontrado: {cohort_path}")
    cohort_ids = [
        line.strip()
        for line in cohort_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(cohort_ids) != len(set(cohort_ids)):
        raise ValueError(f"Hay IDs duplicados en {cohort_path}")
    by_id = {note["paciente_id"]: note for note in notes}
    missing = [patient_id for patient_id in cohort_ids if patient_id not in by_id]
    if missing:
        raise ValueError(
            f"{len(missing)} IDs del manifiesto no están en el Excel: {missing}"
        )
    selected = [by_id[patient_id] for patient_id in cohort_ids]
    print(f"  Cohorte fijada: {len(selected)} IDs desde {cohort_path.name}")
    return selected


# ─────────────────────────────────────────────
# INFERENCIA
# ─────────────────────────────────────────────

def run_inference_direct(
    nota_clinica: str,
    model,
    tokenizer,
    model_family: str,
    system_prompt: str,
    max_new_tokens: int = 8000,
    temperature: float = 0.0,
    top_p: float = 1.0,
    enable_thinking: bool = False,
    thinking_budget: Optional[int] = 1024,
    stop_on_complete_json: bool = False,
    record_raw_response: bool = False,
) -> Tuple[Dict[str, Any], float, Dict[str, Any]]:

    user_content = USER_INSTRUCTIONS_PREFIX + nota_clinica + USER_INSTRUCTIONS_SUFFIX
    messages     = build_messages(system_prompt, user_content, model_family)
    inputs       = apply_template_for_family(
        tokenizer, messages, model_family,
        enable_thinking=enable_thinking,
        thinking_budget=thinking_budget,
    ).to(model.device)
    terminators  = get_terminators(tokenizer, model_family, model=model)

    input_len = inputs["input_ids"].shape[1]
    print(f"    Tokens entrada : {input_len} | thinking: {'ON budget=' + str(thinking_budget) if enable_thinking else 'OFF'}", flush=True)

    gpu_samples: list = []
    stop_event        = threading.Event()
    monitor_thread    = threading.Thread(
        target=monitor_gpu_during_inference,
        args=(stop_event, gpu_samples),
        daemon=True,
    )
    monitor_thread.start()

    ts_inicio = datetime.now(timezone.utc).isoformat()
    t0        = time.time()

    gen_kwargs: dict = {
        "max_new_tokens": max_new_tokens,
        "eos_token_id":   terminators,
        "pad_token_id":   tokenizer.pad_token_id,
    }
    json_stopper = None
    if stop_on_complete_json:
        json_stopper = JsonObjectStoppingCriteria(tokenizer, input_len)
        gen_kwargs["stopping_criteria"] = StoppingCriteriaList([json_stopper])
    if temperature == 0:
        gen_kwargs["do_sample"] = False
    else:
        gen_kwargs["do_sample"]   = True
        gen_kwargs["temperature"] = temperature
        gen_kwargs["top_p"]       = top_p

    with torch.no_grad():
        outputs = model.generate(**inputs, **gen_kwargs)

    elapsed = time.time() - t0
    ts_fin  = datetime.now(timezone.utc).isoformat()
    stop_event.set()
    monitor_thread.join(timeout=2.0)

    tokens_salida = outputs.shape[1] - input_len
    raw_response = tokenizer.decode(outputs[0][input_len:], skip_special_tokens=True)
    json_end = find_complete_json_end(raw_response)
    truncated = tokens_salida >= max_new_tokens
    last_token_id = int(outputs[0][-1].item())
    if json_stopper is not None and json_stopper.completed:
        finish_reason = "json_complete"
    elif last_token_id in terminators:
        finish_reason = "eos"
    elif truncated:
        finish_reason = "max_tokens"
    else:
        finish_reason = "generation_stopped"
    print(
        f"    {elapsed:.1f}s | out={tokens_salida} | fin={finish_reason}"
        f"{' [TRUNCADO]' if truncated else ''}",
        flush=True,
    )

    thinking_text, json_text = separate_thinking(raw_response)

    thinking_tokens = count_tokens(tokenizer, thinking_text) if thinking_text else 0
    json_tokens     = count_tokens(tokenizer, json_text)

    response_clean = json_text if (model_family in (FAMILY_QWEN, FAMILY_GEMMA) and thinking_text) else raw_response
    parsed = _parse_json_response(response_clean)

    infer_meta = {
        "timestamp_inicio":   ts_inicio,
        "timestamp_fin":      ts_fin,
        "latencia_s":         round(elapsed, 3),
        "tokens_entrada":     input_len,
        "tokens_salida":      tokens_salida,
        "thinking_tokens":    thinking_tokens,
        "json_tokens":        json_tokens,
        "truncated":          truncated,
        "finish_reason":      finish_reason,
        "json_complete":      json_end is not None,
        "json_end_character": json_end,
        "characters_after_json": len(raw_response) - json_end if json_end is not None else None,
        "last_token_ids":     [int(v) for v in outputs[0][-12:].tolist()],
        "eos_token_ids":      terminators,
        "tokens_por_segundo": round(tokens_salida / elapsed, 2) if elapsed > 0 else None,
        "enable_thinking":    enable_thinking,
        "thinking_budget":    thinking_budget,
        "temperature":        temperature,
        "top_p":              top_p,
        "gpu_summary":        summarize_gpu_samples(gpu_samples),
        "gpu_timeseries":     gpu_samples,
    }
    if record_raw_response:
        infer_meta["raw_response"] = raw_response
    return parsed, elapsed, infer_meta


def _parse_json_response(response: str) -> Dict[str, Any]:
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass
    md_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, re.DOTALL)
    if md_match:
        try:
            return json.loads(md_match.group(1))
        except json.JSONDecodeError:
            pass
    obj_match = re.search(r'\{.*\}', response, re.DOTALL)
    if obj_match:
        try:
            return json.loads(obj_match.group(0))
        except json.JSONDecodeError:
            pass
    print("    ADVERTENCIA: No se pudo parsear JSON. Guardando respuesta cruda.")
    return {"_raw_response": response}


# ─────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────

def flatten_json(data: dict, prefix: str = "", sep: str = ".") -> dict:
    flat = {}
    for key, val in data.items():
        full_key = f"{prefix}{sep}{key}" if prefix else key
        if isinstance(val, dict):
            flat.update(flatten_json(val, full_key, sep))
        elif isinstance(val, list):
            flat[full_key] = json.dumps(val, ensure_ascii=False)
        else:
            flat[full_key] = val
    return flat


def extract_metadata(resultados: Dict[str, Any]) -> Dict[str, Any]:
    campos = {k: v for k, v in resultados.items() if not k.startswith("_")}
    total  = len(campos)
    if total == 0:
        return {"_campos_totales": 0}
    valores = [v.get("valor") if isinstance(v, dict) else v for v in campos.values()]
    n_true  = sum(1 for v in valores if v is True)
    n_false = sum(1 for v in valores if v is False)
    n_det   = n_true + n_false
    return {
        "_campos_totales":        total,
        "_campos_true":           n_true,
        "_campos_false":          n_false,
        "_campos_null":           sum(1 for v in valores if v is None),
        "_campos_detectados":     n_det,
        "_porcentaje_detectado":  round(n_det / total * 100, 1) if total else 0,
    }


def build_excel_outputs(
    model_name:  str,
    model_dir:   Path,
    jsons_dir:   Path,
    df_original: "pd.DataFrame",
) -> None:
    """Lee todos los JSONs del directorio y genera dos Excel:

    1. resumen_{model_name}.xlsx  — columnas originales + metadatos + campo.valor/evidencia
    2. campos_{model_name}.xlsx   — solo ID + campo.valor/evidencia por campo extraído
    """
    # Cargar todos los JSONs de resultado (excluir _meta)
    paciente_jsons: Dict[str, Any] = {}
    for json_path in sorted(jsons_dir.glob(f"*__{model_name}.json")):
        if "_meta" in json_path.stem:
            continue
        paciente_id = json_path.stem.split("__")[0]
        try:
            paciente_jsons[paciente_id] = json.loads(
                json_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            print(f"  [AVISO] Error leyendo {json_path.name}: {exc}")

    if not paciente_jsons:
        print(f"  [AVISO] Sin JSONs en {jsons_dir} — no se genera Excel")
        return

    # Campos extraídos (orden de primera aparición)
    all_fields: List[str] = []
    for resultado in paciente_jsons.values():
        for key in resultado:
            if not key.startswith("_") and key not in all_fields:
                all_fields.append(key)

    id_to_rowindex = {
        str(row[EXCEL_ID_COL]).strip(): idx
        for idx, row in df_original.iterrows()
    }

    # ── Excel 1: Completo ────────────────────────────────────────────────────
    meta_cols = [
        "_campos_totales", "_campos_true", "_campos_false",
        "_campos_null", "_campos_detectados", "_porcentaje_detectado",
    ]
    field_cols: List[str] = []
    for f in all_fields:
        field_cols += [f"{f}.valor", f"{f}.evidencia"]

    df_completo = df_original.copy()
    for col in meta_cols + field_cols:
        df_completo[col] = None

    for paciente_id, resultado in paciente_jsons.items():
        row_idx = id_to_rowindex.get(paciente_id)
        if row_idx is None:
            print(f"  [AVISO] ID {paciente_id} no encontrado en Excel original")
            continue

        campos = {k: v for k, v in resultado.items() if not k.startswith("_")}
        total  = len(campos)
        vals   = [v.get("valor") if isinstance(v, dict) else v for v in campos.values()]
        n_true  = sum(1 for v in vals if v is True)
        n_false = sum(1 for v in vals if v is False)
        n_det   = n_true + n_false

        df_completo.at[row_idx, "_campos_totales"]       = total
        df_completo.at[row_idx, "_campos_true"]          = n_true
        df_completo.at[row_idx, "_campos_false"]         = n_false
        df_completo.at[row_idx, "_campos_null"]          = sum(1 for v in vals if v is None)
        df_completo.at[row_idx, "_campos_detectados"]    = n_det
        df_completo.at[row_idx, "_porcentaje_detectado"] = (
            round(n_det / total * 100, 1) if total else 0
        )

        for field, val_obj in campos.items():
            if isinstance(val_obj, dict):
                df_completo.at[row_idx, f"{field}.valor"]     = val_obj.get("valor")
                df_completo.at[row_idx, f"{field}.evidencia"] = val_obj.get("evidencia")
            else:
                df_completo.at[row_idx, f"{field}.valor"]     = val_obj
                df_completo.at[row_idx, f"{field}.evidencia"] = None

    excel_completo = model_dir / f"resumen_{model_name}.xlsx"
    df_completo.to_excel(excel_completo, index=False)
    print(f"  Excel completo : {excel_completo}")

    # ── Excel 2: Solo campos + evidencia ─────────────────────────────────────
    rows_campos: List[Dict[str, Any]] = []
    for paciente_id, resultado in paciente_jsons.items():
        row: Dict[str, Any] = {EXCEL_ID_COL: paciente_id}
        for field in all_fields:
            val_obj = resultado.get(field)
            if isinstance(val_obj, dict):
                row[f"{field}.valor"]     = val_obj.get("valor")
                row[f"{field}.evidencia"] = val_obj.get("evidencia")
            else:
                row[f"{field}.valor"]     = val_obj
                row[f"{field}.evidencia"] = None
        rows_campos.append(row)

    df_campos = pd.DataFrame(rows_campos)
    excel_campos = model_dir / f"campos_{model_name}.xlsx"
    df_campos.to_excel(excel_campos, index=False)
    print(f"  Excel campos   : {excel_campos}")
    print(f"  JSONs          : {jsons_dir}")


def save_inference_meta(meta_path: Path, paciente_id: str, model_name: str,
                        model_family: str, infer_meta: Dict[str, Any]) -> None:
    payload = {
        "paciente_id":  paciente_id,
        "model_name":   model_name,
        "model_family": model_family,
        "inferencia":   infer_meta,
    }
    meta_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# ─────────────────────────────────────────────
# EXTRACCIÓN — UN MODELO
# ─────────────────────────────────────────────

def run_experiment_model(
    model_name:      str,
    experiment_dir:  str,
    excel_path:      str,
    fields_file:     str,
    context_file:    str,
    n_epicrisis:     int   = 0,
    max_new_tokens:  int   = 8000,
    temperature:     float = 0.0,
    top_p:           float = 1.0,
    enable_thinking: bool  = False,
    thinking_budget: int   = 1024,
    cohort_ids_file: Optional[str] = None,
    stop_on_complete_json: bool = False,
    record_raw_response: bool = False,
    verbose:         bool  = True,
) -> None:

    exp_dir   = Path(experiment_dir)
    model_dir = exp_dir / model_name
    jsons_dir = model_dir / "jsons"
    model_dir.mkdir(parents=True, exist_ok=True)
    jsons_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*70}")
    print(f"  Experimento : {exp_dir.name}")
    print(f"  Modelo      : {model_name}")
    print(f"  Fields      : {Path(fields_file).name}")
    print(f"  Context     : {Path(context_file).name}")
    print(f"  n_epicrisis : {n_epicrisis if n_epicrisis > 0 else 'todos'}")
    print(f"  temperature : {temperature}  top_p: {top_p}  max_tokens: {max_new_tokens}")
    print(f"{'='*70}\n")

    system_prompt = load_system_prompt(fields_file, context_file)

    # Con manifiesto se carga el Excel completo, se selecciona en el orden
    # congelado y recién entonces se aplica n_epicrisis como límite defensivo.
    notes, excel_p, df_original = load_clinical_notes(
        excel_path, 0 if cohort_ids_file else n_epicrisis
    )
    notes = select_notes_by_cohort(notes, cohort_ids_file)
    if n_epicrisis > 0:
        notes = notes[:n_epicrisis]

    # Auto-resume: detecta JSONs ya procesados por ID de paciente
    existing_ids = {
        p.stem.split("__")[0]
        for p in jsons_dir.glob(f"*__{model_name}.json")
    }
    if existing_ids:
        pending = [n for n in notes if n["paciente_id"] not in existing_ids]
        print(f"  [RESUME] {len(existing_ids)} ya procesados → {len(pending)} pendientes")
        if not pending:
            print("  [RESUME] Experimento ya completo. Solo regenerando Excel.")
            build_excel_outputs(model_name, model_dir, jsons_dir, df_original)
            return
        notes = pending
    else:
        pending = notes

    model, tokenizer, family = load_model(model_name)

    for idx, note in enumerate(notes, 1):
        paciente_id  = note["paciente_id"]
        nota_clinica = note["nota_clinica"]

        print(f"\n  [{idx}/{len(notes)}] {paciente_id}", flush=True)

        if len(nota_clinica.strip()) < 50:
            print("    Nota muy corta o vacía — omitida", flush=True)
            continue

        try:
            resultado, elapsed, infer_meta = run_inference_direct(
                nota_clinica, model, tokenizer, family,
                system_prompt=system_prompt,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                top_p=top_p,
                enable_thinking=enable_thinking,
                thinking_budget=thinking_budget,
                stop_on_complete_json=stop_on_complete_json,
                record_raw_response=record_raw_response,
            )

            metadata = extract_metadata(resultado)

            json_path = jsons_dir / f"{paciente_id}__{model_name}.json"
            json_path.write_text(json.dumps(resultado, indent=2, ensure_ascii=False), encoding="utf-8")

            meta_path = jsons_dir / f"{paciente_id}__{model_name}_meta.json"
            save_inference_meta(meta_path, paciente_id, model_name, family, infer_meta)

            if verbose:
                print(f"    OK: {metadata.get('_campos_true', '?')} positivos / {metadata.get('_campos_totales', '?')} campos", flush=True)

        except Exception as exc:
            print(f"    ERROR: {exc}", flush=True)
            continue

    unload_model(model, model_name)

    build_excel_outputs(model_name, model_dir, jsons_dir, df_original)


# ─────────────────────────────────────────────
# MODO LAUNCHER — lee config.yml y lanza sbatch
# ─────────────────────────────────────────────

def build_all_excels(script_dir: str, experiment: Optional[str] = None) -> None:
    """Recorre todos los experimentos (o uno específico) y genera los dos Excel
    por cada modelo que tenga JSONs, sin necesitar GPU ni torch."""
    try:
        import yaml
    except ImportError:
        print("ERROR: PyYAML no instalado. Ejecuta: pip install pyyaml", file=sys.stderr)
        sys.exit(1)

    script_path = Path(script_dir)
    exp_base    = script_path / "experiments"
    style_py    = script_path / "extraction_postprocess" / "style_excel.py"

    exp_dirs: List[Path] = (
        [exp_base / experiment] if experiment
        else sorted(p for p in exp_base.iterdir() if p.is_dir())
    )

    for exp_dir in exp_dirs:
        config_path = exp_dir / "config.yml"
        if not config_path.exists():
            print(f"  [SKIP] {exp_dir.name}: sin config.yml")
            continue

        cfg        = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        excel_path = cfg["experiment"].get("excel_path", "").strip()

        if not excel_path:
            candidates = sorted((script_path / "excel_resumen").glob("*.xlsx"))
            if not candidates:
                print(f"  [SKIP] {exp_dir.name}: sin excel_path y sin .xlsx en excel_resumen/")
                continue
            excel_path = str(candidates[0])
        else:
            excel_path = str(script_path / excel_path)

        try:
            _, _, df_original = load_clinical_notes(excel_path)
        except (ValueError, FileNotFoundError) as exc:
            print(f"  [SKIP] {exp_dir.name}: excel inválido — {exc}")
            continue

        for model_cfg in cfg["models"]:
            model_name = model_cfg["name"]
            model_dir  = exp_dir / model_name
            jsons_dir  = model_dir / "jsons"

            n_jsons = (
                len([f for f in jsons_dir.glob(f"*__{model_name}.json")
                     if "_meta" not in f.stem])
                if jsons_dir.exists() else 0
            )
            if n_jsons == 0:
                print(f"  [SKIP] {exp_dir.name}/{model_name}: sin JSONs")
                continue

            print(f"\n{'='*60}")
            print(f"  {exp_dir.name} / {model_name}  ({n_jsons} JSONs)")
            print(f"{'='*60}")
            build_excel_outputs(model_name, model_dir, jsons_dir, df_original)

            if style_py.exists():
                for xlsx in [
                    model_dir / f"resumen_{model_name}.xlsx",
                    model_dir / f"campos_{model_name}.xlsx",
                ]:
                    if xlsx.exists():
                        subprocess.run(
                            [sys.executable, str(style_py), str(xlsx)],
                            check=False,
                        )

    print("\nListo.")


def launch_experiment(experiment: str, script_dir: str) -> None:
    try:
        import yaml
    except ImportError:
        print("ERROR: PyYAML no instalado. Ejecuta: pip install pyyaml", file=sys.stderr)
        sys.exit(1)

    script_dir_p = Path(script_dir)
    exp_dir      = script_dir_p / "experiments" / experiment
    config_path  = exp_dir / "config.yml"

    if not config_path.exists():
        print(f"ERROR: {config_path} no encontrado.", file=sys.stderr)
        sys.exit(1)

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    slurm        = cfg.get("slurm", {})
    sl_nodelist  = slurm.get("nodelist",      "ih-condor")
    sl_time      = slurm.get("time",          "00:59:00")
    sl_partition = slurm.get("partition",     "batch")
    sl_qos       = slurm.get("qos",           "batch")
    sl_gpus      = slurm.get("gpus",          1)
    sl_cpus      = slurm.get("cpus_per_task", 10)
    sl_mem       = slurm.get("mem",           "32G")

    fields_name  = cfg["prompt"]["fields"]
    context_name = cfg["prompt"]["context"]
    fields_file  = script_dir_p / "llm-prompts" / "fields"  / f"{fields_name}.txt"
    context_file = script_dir_p / "llm-prompts" / "context" / f"{context_name}.txt"
    n_epicrisis  = cfg["experiment"].get("n_epicrisis", 0)
    excel_path   = cfg["experiment"].get("excel_path", "").strip()
    cohort_ids_rel = str(cfg["experiment"].get("cohort_ids_file", "")).strip()
    cohort_ids_file = (
        str(script_dir_p / cohort_ids_rel) if cohort_ids_rel else ""
    )

    if not excel_path:
        # fallback: primer .xlsx en excel_resumen/
        candidates = sorted((script_dir_p / "excel_resumen").glob("*.xlsx"))
        if not candidates:
            print("ERROR: excel_path vacío y no hay .xlsx en excel_resumen/", file=sys.stderr)
            sys.exit(1)
        excel_path = str(candidates[0])

    for fields_f, label in [(fields_file, "fields"), (context_file, "context")]:
        if not fields_f.exists():
            print(f"ERROR: archivo de prompt no encontrado: {fields_f}", file=sys.stderr)
            sys.exit(1)

    print(f"Experimento : {experiment}")
    print(f"Descripción : {cfg['experiment'].get('description', '')}")
    print(f"n_epicrisis : {n_epicrisis if n_epicrisis > 0 else 'todos'}")
    print(f"Cohorte     : {cohort_ids_file or 'orden del Excel'}")
    print(f"Prompt      : fields={fields_name}  context={context_name}")
    print(f"Excel       : {excel_path}")
    print(f"Modelos     : {[m['name'] for m in cfg['models']]}\n")

    run_py    = script_dir_p / "extraction" / "run_experiment.py"
    style_py  = script_dir_p / "extraction_postprocess" / "style_excel.py"
    report_py = script_dir_p / "monitoring" / "generate_report_monitoring.py"

    submitted = []
    for model_cfg in cfg["models"]:
        model_name = model_cfg["name"]
        hp         = model_cfg.get("hyperparams", {})
        model_dir  = exp_dir / model_name
        jsons_dir  = model_dir / "jsons"

        # Comprobar si ya está completo
        if jsons_dir.is_dir() and n_epicrisis > 0:
            done = len([f for f in jsons_dir.iterdir()
                        if f.suffix == ".json" and "_meta" not in f.name])
            if done >= n_epicrisis:
                print(f"  [SKIP] {model_name}: {done}/{n_epicrisis} JSONs ya completos")
                continue

        model_dir.mkdir(parents=True, exist_ok=True)
        (model_dir / "slurm_logs").mkdir(exist_ok=True)

        thinking_flag = "--enable_thinking" if hp.get("enable_thinking", False) else ""
        stop_json_flag = "--stop_on_complete_json" if hp.get("stop_on_complete_json", False) else ""
        raw_flag = "--record_raw_response" if hp.get("record_raw_response", False) else ""
        cohort_flag = f"--cohort_ids_file {cohort_ids_file}" if cohort_ids_file else ""
        conda_env = os.environ.get(
            "CONDA_ENV",
            os.environ.get("CONDA_DEFAULT_ENV", "newLLM"),
        )

        job_script = f"""#!/bin/bash
# Job auto-generado por run_experiment.py — experimento: {experiment} / modelo: {model_name}

#SBATCH --nodelist={sl_nodelist}
#SBATCH --job-name=exp_{experiment[:12]}_{model_name}
#SBATCH -t {sl_time}
#SBATCH -p {sl_partition}
#SBATCH -q {sl_qos}
#SBATCH --gpus={sl_gpus}
#SBATCH --cpus-per-task={sl_cpus}
#SBATCH --mem={sl_mem}
#SBATCH --output={model_dir}/slurm_logs/slurm_%j.out
#SBATCH --error={model_dir}/slurm_logs/slurm_%j.err

module load micromamba
micromamba activate {conda_env}

set -eo pipefail

python3 {run_py} \\
    --experiment_dir {exp_dir} \\
    --model_name {model_name} \\
    --fields_file {fields_file} \\
    --context_file {context_file} \\
    --excel_path {excel_path} \\
    --n_epicrisis {n_epicrisis} \\
    --temperature {hp.get('temperature', 0)} \\
    --max_new_tokens {hp.get('max_new_tokens', 8000)} \\
    --top_p {hp.get('top_p', 1.0)} \\
    --thinking_budget {hp.get('thinking_budget', 1024)} \\
    {cohort_flag} \\
    {stop_json_flag} \\
    {raw_flag} \\
    {thinking_flag} \\
    --verbose

python3 {style_py} {model_dir}/resumen_{model_name}.xlsx {model_dir}/campos_{model_name}.xlsx

python3 {report_py} {jsons_dir} {model_dir}/reporte_{model_name}.pdf || echo "AVISO: reporte PDF falló"
"""

        job_path = model_dir / "job.sh"
        job_path.write_text(job_script, encoding="utf-8")
        job_path.chmod(0o755)

        result = subprocess.run(["sbatch", str(job_path)], capture_output=True, text=True)
        if result.returncode == 0:
            job_id = result.stdout.strip().split()[-1]
            print(f"  [OK] {model_name} → job {job_id}")
            submitted.append((model_name, job_id))
        else:
            print(f"  [ERROR] {model_name}: {result.stderr.strip()}")

    if submitted:
        print(f"\n{len(submitted)} job(s) enviados:")
        for name, jid in submitted:
            print(f"  {name:12s} → {jid}")
        print(f"\nMonitorear con: squeue -u $USER")
    else:
        print("\nNo se enviaron jobs nuevos.")


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="run_experiment — launcher y extractor")

    # Modo launcher
    parser.add_argument("--launch",     action="store_true", help="Leer config.yml y enviar jobs a SLURM")
    parser.add_argument("--experiment", default=None,        help="Nombre del experimento (modo --launch)")
    parser.add_argument("--script_dir", default=None,        help="Ruta a extraction-condor/ (modo --launch)")

    # Modo build_excel: regenera ambos Excel desde JSONs existentes (un modelo)
    parser.add_argument("--build_excel", action="store_true",
                        help="Regenerar Excel desde JSONs existentes sin correr inferencia")

    # Modo build_all: recorre todos los experimentos y regenera Excel de cada modelo
    parser.add_argument("--build_all", action="store_true",
                        help="Regenerar Excel para todos los modelos de todos los experimentos")

    # Modo extracción (llamado por sbatch)
    parser.add_argument("--experiment_dir", default=None)
    parser.add_argument("--model_name",     default="Gemma4")
    parser.add_argument("--fields_file",    default=None)
    parser.add_argument("--context_file",   default=None)
    parser.add_argument("--excel_path",     default=None)
    parser.add_argument("--n_epicrisis",    type=int,   default=0)
    parser.add_argument("--temperature",    type=float, default=0.0)
    parser.add_argument("--max_new_tokens", type=int,   default=8000)
    parser.add_argument("--top_p",          type=float, default=1.0)
    parser.add_argument("--enable_thinking", action="store_true", default=False)
    parser.add_argument("--thinking_budget", type=int, default=1024)
    parser.add_argument("--cohort_ids_file", default=None)
    parser.add_argument("--stop_on_complete_json", action="store_true", default=False)
    parser.add_argument("--record_raw_response", action="store_true", default=False)
    parser.add_argument("--verbose",        action="store_true")

    args = parser.parse_args()

    if args.launch:
        if not args.experiment:
            print("ERROR: --experiment requerido en modo --launch", file=sys.stderr)
            sys.exit(1)
        script_dir = args.script_dir or str(Path(__file__).resolve().parent.parent)
        launch_experiment(args.experiment, script_dir)

    elif args.build_all:
        script_dir = args.script_dir or str(Path(__file__).resolve().parent.parent)
        build_all_excels(script_dir, args.experiment)

    elif args.build_excel:
        for required in ("experiment_dir", "model_name", "excel_path"):
            if not getattr(args, required):
                print(f"ERROR: --{required} requerido en modo --build_excel", file=sys.stderr)
                sys.exit(1)
        exp_dir   = Path(args.experiment_dir)
        model_dir = exp_dir / args.model_name
        jsons_dir = model_dir / "jsons"
        _, _, df_original = load_clinical_notes(args.excel_path)
        build_excel_outputs(args.model_name, model_dir, jsons_dir, df_original)

    else:
        for required in ("experiment_dir", "model_name", "fields_file", "context_file", "excel_path"):
            if not getattr(args, required):
                print(f"ERROR: --{required} requerido en modo extracción", file=sys.stderr)
                sys.exit(1)

        run_experiment_model(
            model_name      = args.model_name,
            experiment_dir  = args.experiment_dir,
            excel_path      = args.excel_path,
            fields_file     = args.fields_file,
            context_file    = args.context_file,
            n_epicrisis     = args.n_epicrisis,
            max_new_tokens  = args.max_new_tokens,
            temperature     = args.temperature,
            top_p           = args.top_p,
            enable_thinking = args.enable_thinking,
            thinking_budget = args.thinking_budget,
            cohort_ids_file = args.cohort_ids_file,
            stop_on_complete_json = args.stop_on_complete_json,
            record_raw_response = args.record_raw_response,
            verbose         = args.verbose,
        )
