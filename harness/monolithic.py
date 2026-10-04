#!/usr/bin/env python3
"""Arm A of form199: the 199 variables requested in a single call.

Kept apart from runner.py so the harness identity (protocol + runner hashes) does not
change. Same note indexing, instruction text, catalogue content and order, validator,
parser, decoding and retry policy as the harness; only the unit of request differs.
The output budget is explicit (--budget) because one call must hold all 199 answers.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from protocol import (ROOT, VERSION, bundle, evidence_index, validate, canonicalize,
                      digest, protocol_hash, strict_output)
from runner import atomic_json, read_inputs, prepare_model, select_notes

STRATEGY = 'monolithic'
GROUP_SENTENCE = 'responde únicamente las claves del grupo solicitado.'
FORM_SENTENCE = 'responde únicamente las claves del formulario solicitado.'


def render_monolithic_prompt(common, fields):
    # Same catalogue entries, in the same order, as the 18 group prompts concatenated.
    if common.count(GROUP_SENTENCE) != 1:
        raise ValueError('common.md changed: group sentence not found exactly once')
    return (common.replace(GROUP_SENTENCE, FORM_SENTENCE)
            + '\n\n## Formulario completo'
            + '\nLas claves exactas y las definiciones de las 199 variables son:\n'
            + json.dumps(fields, ensure_ascii=False, indent=2)
            + '\n\nLee toda la epicrisis; cada clave conserva su contexto y sus relaciones del catálogo.\n')


def field_level_valid(output, fields, index):
    # Keys whose own entry passes every field-level check; used to report partial forms.
    if not isinstance(output, dict):
        return []
    return [f['key'] for f in fields
            if f['key'] in output and not validate({f['key']: output[f['key']]}, [f], index, relations=False)]


def run_case(row, model_name, settings, directory, infer, identity):
    fields, common = bundle()
    indexed, index = evidence_index(row['text'])
    signature = digest(identity + STRATEGY + row['patient_id'] + row['text'] + model_name
                       + json.dumps(settings, sort_keys=True))
    final_path = directory / 'results' / (row['patient_id'] + '.json')
    if final_path.exists():
        previous = json.loads(final_path.read_text())
        if previous.get('signature') != signature:
            raise ValueError('Result fingerprint changed; use a new output directory')
        return {'valid': previous['valid'], 'valid_fields': previous['valid_fields'], 'new_calls': 0,
                'errors_count': len(previous['errors'])}
    prompt = render_monolithic_prompt(common, fields)
    trace_dir = directory / 'traces' / row['patient_id']
    errors, output, calls = [], None, 0
    for attempt in range(settings['max_retries'] + 1):
        retry = ''
        if attempt:
            retry = ('\n\n## Corrige la respuesta del intento previo\n'
                     + json.dumps({'errors': errors, 'previous_output': output}, ensure_ascii=False)
                     + '\nDevuelve nuevamente el formulario completo. No conviertas fallos en No.')
        output, meta = infer(prompt + retry, indexed, settings['max_new_tokens'])
        calls += 1
        errors = validate(output, fields, index)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        atomic_json(trace_dir / f'form_{stamp}.json',
                    {'signature': signature, 'strategy': STRATEGY, 'attempt': attempt + 1,
                     'timestamp': datetime.now(timezone.utc).isoformat(), 'prompt': prompt + retry,
                     'output': output, 'errors': errors, 'inference': meta})
        if not errors:
            break
    # The whole form is the unit of validation. Valid fields of an invalid form are kept for
    # variable-level analysis; invalid or missing ones stay missing, never "No".
    kept_keys = field_level_valid(output, fields, index)
    kept = {k: output[k] for k in kept_keys}
    result = {'protocol': VERSION, 'strategy': STRATEGY, 'signature': signature,
              'patient_id': row['patient_id'], 'model': model_name,
              'note_sha256': digest(row['text']), 'settings': settings,
              'valid': not errors, 'errors': errors, 'valid_fields': len(kept),
              'new_calls': calls, 'annotations': canonicalize(kept, index)}
    atomic_json(final_path, result)
    return {'valid': result['valid'], 'valid_fields': len(kept), 'new_calls': calls,
            'errors_count': len(errors)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--input-dir', help='Override only for offline dataset verification')
    parser.add_argument('--model', choices=['Gemma4', 'Llama-70B', 'Qwen'])
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--case-id', help='Run a single case chosen explicitly (pilot)')
    parser.add_argument('--budget', type=int, help='max_new_tokens for the single call')
    parser.add_argument('--run', action='store_true', help='Actually load a model; requires SLURM')
    parser.add_argument('--check', action='store_true', help='Verify bundle and input without inference')
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(args.config.read_text())
    fields, common = bundle()
    if json.loads((ROOT / 'manifest.json').read_text())['protocol_sha256'] != protocol_hash():
        raise ValueError('Prompts out of date: regenerate with protocol.py')
    prompt = render_monolithic_prompt(common, fields)
    notes = select_notes(read_inputs(config, args.input_dir), args.limit, args.case_id)
    print(json.dumps({'protocol': VERSION, 'strategy': STRATEGY, 'variables': len(fields),
                      'prompt_characters': len(prompt), 'cohort_cases': 50,
                      'selected_cases': len(notes), 'inference_requested': args.run}))
    if not args.run or args.check:
        return
    if not os.environ.get('SLURM_JOB_ID'):
        raise RuntimeError('Inference must run inside a SLURM allocation')
    if args.input_dir:
        raise ValueError('Input override is check-only; version a config to change inference data')
    if args.model not in config['models']:
        raise ValueError('Choose a configured model')
    if not args.budget or args.budget <= 0:
        raise ValueError('Set --budget explicitly')
    sys.path.insert(0, str(ROOT.parent / 'extraction'))
    from run_experiment import load_model, unload_model, run_inference_direct
    settings = {**config['generation'], 'max_new_tokens': args.budget}
    directory = Path(config['output_dir'] + '-monolithic') / f'{args.model}-b{args.budget}'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_handle = (directory / '.run.lock').open('a')
    fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    identity = (protocol_hash() + digest(Path(__file__).read_text())
                + digest((ROOT / 'runner.py').read_text())
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
        # v1.1: the legacy tolerant parse is kept only as metadata; the strict parse decides.
        meta['legacy_parser_object'] = isinstance(output, dict) and '_raw_response' not in output
        meta.update(inference_setup)
        return strict_output(meta)
    try:
        for number, row in enumerate(notes, 1):
            summary = run_case(row, args.model, settings, directory, infer, identity)
            print(json.dumps({'case_number': number, **summary}), flush=True)
    finally:
        unload_model(model, args.model)


if __name__ == '__main__':
    main()
