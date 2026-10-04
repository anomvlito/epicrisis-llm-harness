#!/usr/bin/env python3
"""Arm C of form199: the harness of Arm B preceded by one call that builds an evidence-grounded
case model, shared by every group call.

Call 0 returns the case model (prompts/case_model.md), validated here. The 18 group calls are
those of runner.run_case with the case model inserted after the common rules. If the case model
remains invalid after retries the case fails for Arm C; the groups are never run without it.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from protocol import (ROOT, VERSION, bundle, evidence_index, digest, protocol_hash, strict_output)
from runner import atomic_json, read_inputs, prepare_model, run_case, select_notes

STRATEGY = 'armc'
CASE_MODEL_PROMPT = ROOT / 'prompts' / 'case_model.md'
CASE_MODEL_SCHEMA = ROOT / 'case_model_schema.json'
# Revision 2 (after the pilot of 24-09-2026): stricter use of doubts and of absent supports.
ARMC_VERSION = 'armc-r3'
CASE_MODEL_PREAMBLE = (
    '\n\n## Modelo del caso (generado automáticamente; puede contener errores)\n'
    'Es una ayuda para leer el episodio con los mismos límites en todas las llamadas. '
    'La epicrisis manda: si el modelo del caso contradice la nota, sigue la nota y explica el '
    'conflicto en `comentario`. Los `evidence_ids` deben citar líneas de la epicrisis, nunca este resumen.\n'
    '- Los elementos de `dudas` y los problemas con certeza distinta de "afirmado" no son hechos: no '
    'marques Sí basándote en ellos; decide con la nota y las reglas (No o ?).\n'
    '- Un soporte, una falla o una infección que el modelo del caso no registra y que tampoco encuentras '
    'en la nota no ocurrió: su variable es No y sus datos dependientes (fechas, motivos, tipo) son null con '
    '`motivo_nulo="no_aplica"`, no `no_documentado`.\n')

ENUMS = {
    'momento': {'antes_uci', 'durante_primera_uci', 'despues_primera_uci', 'sin_uci'},
    'tipo': {'ingreso', 'traslado', 'procedimiento', 'soporte', 'infeccion', 'complicacion',
             'falla_organica', 'egreso', 'otro'},
    'certeza': {'afirmado', 'negado', 'sospecha_descartada', 'sospecha_no_resuelta'},
    'estado': {'antecedente_cronico', 'agudo_del_episodio'},
}
TOP_KEYS = {'episodio', 'linea_de_tiempo', 'problemas', 'soportes', 'infecciones',
            'desenlace_primera_uci', 'calidad_documental', 'dudas'}


def _date(value):
    if value is None:
        return True
    if not isinstance(value, str) or not re.fullmatch(r'\d{2}/\d{2}/\d{4}', value):
        return False
    try:
        datetime.strptime(value, '%d/%m/%Y')
        return True
    except ValueError:
        return False


def _text(value, nullable=False):
    return (value is None and nullable) or (isinstance(value, str) and bool(value.strip()))


def validate_case_model(cm, index):
    if not isinstance(cm, dict):
        return ['root: expected exactly one JSON object (strict parser)']
    errors = []
    errors += [f'{k}: missing section' for k in sorted(TOP_KEYS - cm.keys())]
    errors += [f'{k}: unexpected section' for k in sorted(cm.keys() - TOP_KEYS)]
    if errors:
        return errors

    def items(section, keys, checks):
        value = cm[section]
        if not isinstance(value, list):
            errors.append(f'{section}: expected list')
            return
        for n, item in enumerate(value):
            where = f'{section}[{n}]'
            if not isinstance(item, dict) or set(item) != keys:
                errors.append(f'{where}: properties must be exactly {sorted(keys)}')
                continue
            ids = item['evidence_ids']
            if not (isinstance(ids, list) and ids and all(isinstance(i, str) and i in index for i in ids)):
                errors.append(f'{where}: evidence_ids must be a non-empty list of existing lines')
            for field, ok in checks(item):
                if not ok:
                    errors.append(f'{where}.{field}: invalid value')

    ep = cm['episodio']
    if not isinstance(ep, dict) or set(ep) != {'hubo_estadia_uci', 'estadias_uci', 'primera_estadia'}:
        errors.append('episodio: properties must be exactly hubo_estadia_uci, estadias_uci, primera_estadia')
    else:
        stays = ep['estadias_uci']
        if not isinstance(ep['hubo_estadia_uci'], bool) or not isinstance(stays, list):
            errors.append('episodio: hubo_estadia_uci must be boolean and estadias_uci a list')
        else:
            for n, stay in enumerate(stays):
                where = f'episodio.estadias_uci[{n}]'
                keys = {'orden', 'ingreso', 'egreso', 'origen', 'destino', 'evidence_ids'}
                if not isinstance(stay, dict) or set(stay) != keys:
                    errors.append(f'{where}: properties must be exactly {sorted(keys)}')
                    continue
                ids = stay['evidence_ids']
                if not (isinstance(ids, list) and ids and all(isinstance(i, str) and i in index for i in ids)):
                    errors.append(f'{where}: evidence_ids must be a non-empty list of existing lines')
                if type(stay['orden']) is not int or not _date(stay['ingreso']) or not _date(stay['egreso']):
                    errors.append(f'{where}: orden must be integer and dates DD/MM/AAAA or null')
                if not _text(stay['origen'], True) or not _text(stay['destino'], True):
                    errors.append(f'{where}: origen/destino must be text or null')
            orders = [s.get('orden') for s in stays if isinstance(s, dict)]
            if ep['hubo_estadia_uci'] and (not stays or ep['primera_estadia'] not in orders):
                errors.append('episodio: an ICU stay requires at least one stay and primera_estadia among them')
            if not ep['hubo_estadia_uci'] and (stays or ep['primera_estadia'] is not None):
                errors.append('episodio: without ICU stay, estadias_uci must be empty and primera_estadia null')

    items('linea_de_tiempo', {'momento', 'dia_relativo', 'evento', 'tipo', 'certeza', 'evidence_ids'},
          lambda i: [('momento', i['momento'] in ENUMS['momento']), ('tipo', i['tipo'] in ENUMS['tipo']),
                     ('certeza', i['certeza'] in ENUMS['certeza']), ('evento', _text(i['evento'])),
                     ('dia_relativo', i['dia_relativo'] is None or type(i['dia_relativo']) is int)])
    items('problemas', {'problema', 'estado', 'certeza', 'evidence_ids'},
          lambda i: [('problema', _text(i['problema'])), ('estado', i['estado'] in ENUMS['estado']),
                     ('certeza', i['certeza'] in ENUMS['certeza'])])
    items('soportes', {'soporte', 'realizado', 'inicio', 'fin', 'evidence_ids'},
          lambda i: [('soporte', _text(i['soporte'])), ('realizado', type(i['realizado']) is bool),
                     ('inicio', _date(i['inicio'])), ('fin', _date(i['fin']))])
    items('infecciones', {'diagnostico', 'sepsis', 'foco', 'germen', 'tratamiento', 'evidence_ids'},
          lambda i: [('diagnostico', _text(i['diagnostico'])),
                     ('sepsis', type(i['sepsis']) is bool or i['sepsis'] == 'unknown'),
                     ('foco', _text(i['foco'], True)), ('germen', _text(i['germen'], True)),
                     ('tratamiento', _text(i['tratamiento'], True))])
    items('dudas', {'tema', 'por_que', 'evidence_ids'},
          lambda i: [('tema', _text(i['tema'])), ('por_que', _text(i['por_que']))])

    out = cm['desenlace_primera_uci']
    if not isinstance(out, dict) or set(out) != {'estado_vital', 'destino', 'evidence_ids'}:
        errors.append('desenlace_primera_uci: properties must be exactly estado_vital, destino, evidence_ids')
    else:
        ids = out['evidence_ids']
        if out['estado_vital'] not in ('Vivo', 'Fallecido', None) or not _text(out['destino'], True):
            errors.append('desenlace_primera_uci: estado_vital must be Vivo, Fallecido or null; destino text or null')
        if not (isinstance(ids, list) and all(isinstance(i, str) and i in index for i in ids)):
            errors.append('desenlace_primera_uci: evidence_ids must reference existing lines')
        elif (out['estado_vital'] is not None or out['destino'] is not None) and not ids:
            errors.append('desenlace_primera_uci: a documented outcome requires evidence')
        no_icu = isinstance(ep, dict) and ep.get('hubo_estadia_uci') is False
        if no_icu and (out['estado_vital'] is not None or out['destino'] is not None):
            errors.append('desenlace_primera_uci: must be null without an ICU stay')

    q = cm['calidad_documental']
    keys = {'secciones_vacias', 'abreviaturas', 'contradicciones', 'no_documentado'}
    if not isinstance(q, dict) or set(q) != keys:
        errors.append(f'calidad_documental: properties must be exactly {sorted(keys)}')
    else:
        if not (isinstance(q['secciones_vacias'], list) and all(_text(x) for x in q['secciones_vacias'])):
            errors.append('calidad_documental.secciones_vacias: list of text')
        if not (isinstance(q['no_documentado'], list) and all(_text(x) for x in q['no_documentado'])):
            errors.append('calidad_documental.no_documentado: list of text')
        if not (isinstance(q['abreviaturas'], dict) and all(_text(k) and _text(v) for k, v in q['abreviaturas'].items())):
            errors.append('calidad_documental.abreviaturas: object of text to text')
        c = q['contradicciones']
        if not (isinstance(c, list) and all(isinstance(x, dict) and set(x) == {'descripcion', 'evidence_ids'}
                                            and _text(x['descripcion']) and isinstance(x['evidence_ids'], list)
                                            and x['evidence_ids'] and all(i in index for i in x['evidence_ids'])
                                            for x in c)):
            errors.append('calidad_documental.contradicciones: list of {descripcion, evidence_ids} with evidence')
    return errors


def case_model_block(cm):
    return CASE_MODEL_PREAMBLE + '```json\n' + json.dumps(cm, ensure_ascii=False, indent=1) + '\n```\n'


def run_case_c(row, model_name, settings, directory, infer, identity):
    """Call 0 with retries, then the 18 group calls of runner.run_case with the case model."""
    fields, common = bundle()
    indexed, index = evidence_index(row['text'])
    instruction = common + '\n\n' + CASE_MODEL_PROMPT.read_text()
    signature = digest(identity + STRATEGY + row['patient_id'] + row['text'] + model_name
                       + json.dumps(settings, sort_keys=True))
    cm_path = directory / 'case_models' / (row['patient_id'] + '.json')
    calls = 0
    if cm_path.exists():
        saved = json.loads(cm_path.read_text())
        if saved.get('signature') != signature:
            raise ValueError('Case-model fingerprint changed; use a new output directory')
        cm, errors = saved['case_model'], saved['errors']
    else:
        errors, cm = [], None
        for attempt in range(settings['max_retries'] + 1):
            retry = ''
            if attempt:
                retry = ('\n\n## Corrige el modelo del caso del intento previo\n'
                         + json.dumps({'errors': errors, 'previous_output': cm}, ensure_ascii=False)
                         + '\nDevuelve nuevamente el modelo del caso completo.')
            cm, meta = infer(instruction + retry, indexed, settings['case_model_max_new_tokens'])
            calls += 1
            errors = validate_case_model(cm, index)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
            atomic_json(directory / 'case_models' / 'traces' / row['patient_id'] / f'case_model_{stamp}.json',
                        {'signature': signature, 'attempt': attempt + 1, 'prompt': instruction + retry,
                         'output': cm, 'errors': errors, 'inference': meta,
                         'timestamp': datetime.now(timezone.utc).isoformat()})
            if not errors:
                break
        atomic_json(cm_path, {'signature': signature, 'case_model': cm, 'errors': errors, 'calls': calls})
    if errors:
        result = {'protocol': VERSION, 'strategy': STRATEGY, 'signature': signature,
                  'patient_id': row['patient_id'], 'model': model_name, 'note_sha256': digest(row['text']),
                  'valid': False, 'errors': ['case_model: ' + e for e in errors], 'valid_fields': 0,
                  'new_calls': calls, 'case_model_valid': False, 'annotations': {}}
        atomic_json(directory / 'results' / (row['patient_id'] + '.json'), result)
        return {'valid': False, 'valid_fields': 0, 'new_calls': calls, 'errors_count': len(errors),
                'case_model_valid': False}
    # The case model enters the group identity, so checkpoints never mix different case models.
    summary = run_case(row, model_name, settings, directory, infer,
                       identity + STRATEGY + digest(json.dumps(cm, sort_keys=True)),
                       context=case_model_block(cm))
    result_path = directory / 'results' / (row['patient_id'] + '.json')
    result = json.loads(result_path.read_text())
    result.update({'strategy': STRATEGY, 'strategy_version': ARMC_VERSION, 'case_model_valid': True,
                   'case_model_calls': calls})
    atomic_json(result_path, result)
    return {**summary, 'new_calls': summary['new_calls'] + calls, 'case_model_valid': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--input-dir', help='Override only for offline dataset verification')
    parser.add_argument('--model', choices=['Gemma4', 'Llama-70B', 'Qwen'])
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--case-id', help='Run a single case chosen explicitly (pilot)')
    parser.add_argument('--run', action='store_true', help='Actually load a model; requires SLURM')
    parser.add_argument('--check', action='store_true', help='Verify bundle and input without inference')
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(args.config.read_text())
    fields, common = bundle()
    if json.loads((ROOT / 'manifest.json').read_text())['protocol_sha256'] != protocol_hash():
        raise ValueError('Prompts out of date: regenerate with protocol.py')
    json.loads(CASE_MODEL_SCHEMA.read_text())
    notes = select_notes(read_inputs(config, args.input_dir), args.limit, args.case_id)
    print(json.dumps({'protocol': VERSION, 'strategy': STRATEGY, 'strategy_version': ARMC_VERSION,
                      'variables': len(fields),
                      'case_model_prompt_characters': len(common) + len(CASE_MODEL_PROMPT.read_text()),
                      'selected_cases': len(notes), 'inference_requested': args.run}))
    if not args.run or args.check:
        return
    if not os.environ.get('SLURM_JOB_ID'):
        raise RuntimeError('Inference must run inside a SLURM allocation')
    if args.input_dir:
        raise ValueError('Input override is check-only; version a config to change inference data')
    if args.model not in config['models']:
        raise ValueError('Choose a configured model')
    sys.path.insert(0, str(ROOT.parent / 'extraction'))
    from run_experiment import load_model, unload_model, run_inference_direct
    settings = config['generation']
    directory = Path(config['output_dir'] + '-' + ARMC_VERSION) / args.model
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_handle = (directory / '.run.lock').open('a')
    fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    identity = (protocol_hash() + digest(Path(__file__).read_text())
                + digest((ROOT / 'runner.py').read_text())
                + digest(CASE_MODEL_PROMPT.read_text()) + digest(CASE_MODEL_SCHEMA.read_text())
                + digest((ROOT.parent / 'extraction/run_experiment.py').read_text()))
    model, tokenizer, family = load_model(args.model)
    inference_setup = prepare_model(model, settings)
    print(json.dumps({'inference_setup': inference_setup}), flush=True)
    def infer(prompt, note, budget):
        output, _, meta = run_inference_direct(
            note, model, tokenizer, family, system_prompt=prompt,
            max_new_tokens=budget, temperature=0.0, top_p=1.0,
            enable_thinking=False, thinking_budget=None,
            stop_on_complete_json=True, record_raw_response=True)
        meta['legacy_parser_object'] = isinstance(output, dict) and '_raw_response' not in output
        meta.update(inference_setup)
        return strict_output(meta)
    try:
        for number, row in enumerate(notes, 1):
            summary = run_case_c(row, args.model, settings, directory, infer, identity)
            print(json.dumps({'case_number': number, **summary}), flush=True)
    finally:
        unload_model(model, args.model)


if __name__ == '__main__':
    main()
