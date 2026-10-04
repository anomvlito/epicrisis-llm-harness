#!/usr/bin/env python3
"""Runner dedicated to form199 (harness arm); --check never imports model libraries."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from protocol import (ROOT, VERSION, bundle, groups, render_prompt, evidence_index,
                      validate, canonicalize, digest, protocol_hash, strict_output)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


class CacheLayout:
    """The fields DynamicCache reads, without num_kv_shared_layers. Transformers 5.5.4 slices
    layer_types[:-num_kv_shared_layers], which is empty when the value is 0 (Gemma 4), so every
    sliding-window layer would keep the whole sequence in its cache."""
    def __init__(self, cfg):
        self.sliding_window = cfg.sliding_window
        self.layer_types = list(cfg.layer_types)
        self.num_hidden_layers = cfg.num_hidden_layers

    def get_text_config(self, decoder=True):
        return self


def prepare_model(model, generation, cache_factory=None):
    """v1.2: identical inference path for both arms. Chunked prefill avoids materialising the full
    attention matrix; the cache layout fix only applies when the transformers slicing bug would
    drop every layer type. Neither changes the computed attention; both reduce memory."""
    model.generation_config.prefill_chunk_size = generation.get('prefill_chunk_size')
    cfg = model.config.get_text_config(decoder=True)
    cache_fix = getattr(cfg, 'num_kv_shared_layers', None) == 0 and bool(getattr(cfg, 'layer_types', None))
    if cache_fix:
        if cache_factory is None:
            from transformers.cache_utils import DynamicCache
            cache_factory = DynamicCache
        layout = CacheLayout(cfg)
        original = model.generate
        def generate(*args, **kwargs):
            if kwargs.get('past_key_values') is None:
                kwargs['past_key_values'] = cache_factory(config=layout)
            return original(*args, **kwargs)
        model.generate = generate
    return {'prefill_chunk_size': model.generation_config.prefill_chunk_size, 'cache_layout_fix': cache_fix}


def select_notes(notes, limit=0, case_id=None):
    """Explicit case selection for pilots (--case-id) or the first N cases (--limit)."""
    if case_id:
        chosen = [row for row in notes if row['patient_id'] == case_id]
        if len(chosen) != 1:
            raise ValueError('case-id not in the frozen cohort')
        return chosen
    if limit < 0 or limit > len(notes):
        raise ValueError('limit outside cohort size')
    return notes[:limit] if limit else notes


def read_inputs(config, input_dir=None):
    directory = Path(input_dir or config['input_dir']).resolve()
    ids = [line.strip() for line in (directory / 'cohort_ids.txt').read_text().splitlines()
           if line.strip() and not line.lstrip().startswith('#')]
    notes = [json.loads(line) for line in (directory / 'notes.jsonl').read_text().splitlines() if line.strip()]
    manifest = json.loads((directory / 'documents_manifest.json').read_text())
    by_id = {row['patient_id']: row for row in notes}
    hashes = {row['patient_id']: row['text_sha256'] for row in manifest}
    if len(ids) != 50 or len(set(ids)) != 50 or len(notes) != 50 or set(by_id) != set(ids) or set(hashes) != set(ids):
        raise ValueError('Expected the exact frozen 50-case cohort')
    if not all(re.fullmatch(r'[A-Za-z0-9_-]+', pid) for pid in ids):
        raise ValueError('Unsafe patient identifier')
    for pid in ids:
        note = by_id[pid]['text']
        if not isinstance(note, str) or not note.strip() or digest(note) != hashes[pid]:
            raise ValueError('Dataset text missing or changed since export')
    snapshot_schema = json.loads((directory / 'form_schema.json').read_text())
    if snapshot_schema != json.loads((ROOT / 'form_schema.json').read_text()):
        raise ValueError('Dataset and prompt schemas differ')
    if (directory / 'manual-anotacion.md').read_text() != (ROOT / 'manual-anotacion.md').read_text():
        raise ValueError('Dataset and prompt manuals differ')
    return [by_id[pid] for pid in ids]


def run_case(row, model_name, settings, directory, infer, identity, context=''):
    # context (Arm C) is inserted after the common rules of every group prompt.
    fields, common = bundle()
    indexed, index = evidence_index(row['text'])
    signature = digest(identity + row['patient_id'] + row['text'] + model_name
                       + json.dumps(settings, sort_keys=True))
    final_path = directory / 'results' / (row['patient_id'] + '.json')
    checkpoint_dir = directory / 'checkpoints' / row['patient_id']
    merged = {}
    calls = 0
    for group in groups(fields):
        checkpoint_path = checkpoint_dir / (group['name'] + '.json')
        if checkpoint_path.exists():
            checkpoint = json.loads(checkpoint_path.read_text())
            if checkpoint.get('signature') != signature:
                raise ValueError('Checkpoint fingerprint changed; use a new output directory')
            candidate = checkpoint.get('output')
            if not validate(candidate, group['fields'], index, relations=False):
                merged.update(candidate)
                continue
        prompt = render_prompt(common + context, group)
        errors = []
        output = None
        for attempt in range(settings['max_retries'] + 1):
            retry = ''
            if attempt:
                retry = ('\n\n## Corrige la respuesta del intento previo\n'
                         + json.dumps({'errors': errors, 'previous_output': output}, ensure_ascii=False)
                         + '\nDevuelve nuevamente el grupo completo. No conviertas fallos en No.')
            output, meta = infer(prompt + retry, indexed, settings['max_new_tokens'])
            calls += 1
            errors = validate(output, group['fields'], index, relations=False)
            trace = {'signature': signature, 'group': group['name'], 'attempt': attempt + 1,
                     'timestamp': datetime.now(timezone.utc).isoformat(), 'prompt': prompt + retry,
                     'output': output, 'errors': errors, 'inference': meta}
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
            atomic_json(checkpoint_dir / 'traces' / f'{group["name"]}_{stamp}.json', trace)
            atomic_json(checkpoint_path, trace)
            if not errors:
                break
        if not errors:
            merged.update(output)
    errors = validate(merged, fields, index)
    result = {'protocol': VERSION, 'signature': signature, 'patient_id': row['patient_id'],
              'model': model_name, 'note_sha256': digest(row['text']),
              'valid': not errors, 'errors': errors, 'valid_fields': len(merged),
              'new_calls': calls, 'annotations': canonicalize(merged, index)}
    # An invalid result remains explicit. The human reference is never read here.
    atomic_json(final_path, result)
    return {'valid': result['valid'], 'valid_fields': len(merged), 'new_calls': calls,
            'errors_count': len(errors)}


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
    generated = json.loads((ROOT / 'manifest.json').read_text())
    if generated['protocol_sha256'] != protocol_hash():
        raise ValueError('Prompts out of date: regenerate with protocol.py')
    for group in groups(fields):
        if (ROOT / 'prompts' / (group['name'] + '.md')).read_text() != render_prompt(common, group):
            raise ValueError('Generated prompt differs from protocol')
    notes = select_notes(read_inputs(config, args.input_dir), args.limit, args.case_id)
    print(json.dumps({'protocol': VERSION, 'variables': len(fields), 'groups': len(groups(fields)),
                      'cohort_cases': 50, 'selected_cases': len(notes), 'inference_requested': args.run}))
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
    directory = Path(config['output_dir']) / args.model
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_handle = (directory / '.run.lock').open('a')
    fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    identity = (protocol_hash() + digest(Path(__file__).read_text())
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
