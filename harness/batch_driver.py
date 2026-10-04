#!/usr/bin/env python3
"""form199 v2.0: the three arms run in rounds on a batched engine (vLLM).

Prompts, validation, retries, evidence handling and result files are those of runner.py (Arm B),
monolithic.py (Arm A) and arm_c.py (Arm C). The only change is scheduling: every pending call of every
selected case is sent in one batch per round, and failed units are retried in the next round with
their errors and previous output. Trace and result layouts match the 1.x runners, so
summarize_runs.py reads both.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from protocol import (ROOT, VERSION, bundle, groups, render_prompt, evidence_index, validate,
                      canonicalize, digest, protocol_hash, strict_output, normalize_boolean_labels)
from runner import atomic_json, read_inputs, select_notes
from monolithic import render_monolithic_prompt, field_level_valid
from arm_c import (validate_case_model, case_model_block, CASE_MODEL_PROMPT, CASE_MODEL_SCHEMA,
                   ARMC_VERSION)

PIPELINE_VERSION = 'form199-v2.6'  # protocol 1.9 on the vLLM engine
ARMS = ('harness', 'armc', 'monolithic')
RETRY = {
    'group': ('\n\n## Corrige la respuesta del intento previo\n', '\nDevuelve nuevamente el grupo completo. '
              'No conviertas fallos en No.'),
    'form': ('\n\n## Corrige la respuesta del intento previo\n', '\nDevuelve nuevamente el formulario completo. '
             'No conviertas fallos en No.'),
    'case_model': ('\n\n## Corrige el modelo del caso del intento previo\n',
                   '\nDevuelve nuevamente el modelo del caso completo.'),
}


def now():
    return datetime.now(timezone.utc)


def field_types():
    return {f['key']: f['type'] for f in bundle()[0]}


class Case:
    def __init__(self, row, directory, identity, model_name):
        self.row, self.pid, self.directory = row, row['patient_id'], directory
        self.indexed, self.index = evidence_index(row['text'])
        self.signature = digest(identity + self.pid + row['text'] + model_name)
        self.result_path = directory / 'results' / (self.pid + '.json')

    def finished(self):
        if not self.result_path.exists():
            return False
        if json.loads(self.result_path.read_text()).get('signature') != self.signature:
            raise ValueError('Result fingerprint changed; use a new output directory')
        return True


class Unit:
    """One call that must end valid or exhaust its retries: a group, a form or a case model."""
    def __init__(self, case, kind, name, prompt, budget, check):
        self.case, self.kind, self.name, self.prompt, self.budget, self.check = case, kind, name, prompt, budget, check
        self.attempts, self.output, self.errors, self.done, self.valid = 0, None, [], False, False

    def request(self):
        text = self.prompt
        if self.attempts:
            head, tail = RETRY[self.kind]
            text += head + json.dumps({'errors': self.errors, 'previous_output': self.output},
                                      ensure_ascii=False) + tail
        return text, self.case.indexed, self.budget

    def trace_path(self, stamp):
        d, pid = self.case.directory, self.case.pid
        if self.kind == 'group':
            return d / 'checkpoints' / pid / 'traces' / f'{self.name}_{stamp}.json'
        if self.kind == 'form':
            return d / 'traces' / pid / f'form_{stamp}.json'
        return d / 'case_models' / 'traces' / pid / f'case_model_{stamp}.json'


def run_rounds(units, engine, max_retries):
    """Batch all pending units; retry failures in later rounds. Returns the number of rounds."""
    rounds = 0
    while True:
        pending = [u for u in units if not u.done]
        if not pending:
            return rounds
        rounds += 1
        requests = [u.request() for u in pending]
        for unit, request, (raw, meta) in zip(pending, requests, engine.generate(requests)):
            output, meta = strict_output({**meta, 'raw_response': raw})
            if unit.kind in ('group', 'form'):
                meta['boolean_labels_normalized'] = normalize_boolean_labels(output, field_types())
            unit.output, unit.attempts = output, unit.attempts + 1
            unit.errors = unit.check(output)
            trace = {'signature': unit.case.signature, 'pipeline': PIPELINE_VERSION, 'attempt': unit.attempts,
                     'round': rounds, 'timestamp': now().isoformat(), 'prompt': request[0], 'output': output,
                     'errors': unit.errors, 'inference': meta}
            if unit.kind == 'group':
                trace['group'] = unit.name
            atomic_json(unit.trace_path(now().strftime('%Y%m%dT%H%M%S%f')), trace)
            if unit.kind == 'group':
                atomic_json(unit.case.directory / 'checkpoints' / unit.case.pid / f'{unit.name}.json', trace)
            if not unit.errors:
                unit.done = unit.valid = True
            elif unit.attempts > max_retries:
                unit.done = True


def prepare_case_dirs(case):
    """A case without a result starts clean; partial traces of an interrupted job are set aside."""
    stamp = now().strftime('%Y%m%dT%H%M%S')
    for path in (case.directory / 'checkpoints' / case.pid, case.directory / 'traces' / case.pid,
                 case.directory / 'case_models' / 'traces' / case.pid):
        if path.exists():
            shutil.move(str(path), str(path) + f'.incomplete-{stamp}')
    cm_path = case.directory / 'case_models' / (case.pid + '.json')
    if cm_path.exists():
        shutil.move(str(cm_path), str(cm_path) + f'.incomplete-{stamp}')


def group_units(case, common, fields_by_group, budget, context=''):
    return [Unit(case, 'group', g['name'], render_prompt(common + context, g), budget,
                 lambda out, g=g, c=case: validate(out, g['fields'], c.index, relations=False))
            for g in fields_by_group]


def base_result(case, model_name, strategy, engine_setup, settings, errors, merged, calls):
    return {'protocol': VERSION, 'pipeline': PIPELINE_VERSION, 'strategy': strategy, 'signature': case.signature,
            'patient_id': case.pid, 'model': model_name, 'note_sha256': digest(case.row['text']),
            'engine': engine_setup, 'settings': settings, 'valid': not errors, 'errors': errors,
            'valid_fields': len(merged), 'new_calls': calls, 'annotations': canonicalize(merged, case.index)}


def run_harness(cases, engine, settings, model_name, fields, common, context_by_case=None, strategy='harness'):
    by_group = groups(fields)
    units_by_case = {c.pid: group_units(c, common, by_group, settings['max_new_tokens'],
                                        (context_by_case or {}).get(c.pid, '')) for c in cases}
    run_rounds([u for us in units_by_case.values() for u in us], engine, settings['max_retries'])
    for case in cases:
        units = units_by_case[case.pid]
        merged = {}
        for unit in units:
            if unit.valid:
                merged.update(unit.output)
        errors = validate(merged, fields, case.index)
        result = base_result(case, model_name, strategy, engine.setup, settings, errors, merged,
                             sum(u.attempts for u in units))
        yield case, result


def run_monolithic(cases, engine, settings, model_name, fields, common):
    prompt = render_monolithic_prompt(common, fields)
    units = {c.pid: Unit(c, 'form', 'form', prompt, settings['monolithic_max_new_tokens'],
                         lambda out, c=c: validate(out, fields, c.index)) for c in cases}
    run_rounds(list(units.values()), engine, settings['max_retries'])
    for case in cases:
        unit = units[case.pid]
        kept_keys = field_level_valid(unit.output, fields, case.index)
        kept = {k: unit.output[k] for k in kept_keys}
        yield case, base_result(case, model_name, 'monolithic', engine.setup, settings, unit.errors, kept,
                                unit.attempts)


def run_armc(cases, engine, settings, model_name, fields, common):
    instruction = common + '\n\n' + CASE_MODEL_PROMPT.read_text()
    cm_units = {c.pid: Unit(c, 'case_model', 'case_model', instruction, settings['case_model_max_new_tokens'],
                            lambda out, c=c: validate_case_model(out, c.index)) for c in cases}
    run_rounds(list(cm_units.values()), engine, settings['max_retries'])
    ready, context = [], {}
    for case in cases:
        unit = cm_units[case.pid]
        atomic_json(case.directory / 'case_models' / (case.pid + '.json'),
                    {'signature': case.signature, 'case_model': unit.output, 'errors': unit.errors,
                     'calls': unit.attempts})
        if unit.valid:
            ready.append(case)
            context[case.pid] = case_model_block(unit.output)
        else:
            result = base_result(case, model_name, 'armc', engine.setup, settings,
                                 ['case_model: ' + e for e in unit.errors], {}, unit.attempts)
            yield case, {**result, 'strategy_version': ARMC_VERSION, 'case_model_valid': False}
    for case, result in run_harness(ready, engine, settings, model_name, fields, common, context, 'armc'):
        calls = cm_units[case.pid].attempts
        yield case, {**result, 'new_calls': result['new_calls'] + calls, 'strategy_version': ARMC_VERSION,
                     'case_model_valid': True, 'case_model_calls': calls}


RUNNERS = {'harness': run_harness, 'monolithic': run_monolithic, 'armc': run_armc}


def arm_directory(config, arm, model_name, settings):
    name = {'harness': 'harness', 'armc': ARMC_VERSION,
            'monolithic': f"monolithic-b{settings['monolithic_max_new_tokens']}"}[arm]
    return Path(config['output_dir']) / name / model_name


def code_identity(settings, arm):
    files = ['batch_driver.py', 'engine_vllm.py', 'runner.py', 'monolithic.py', 'arm_c.py',
             'prompts/case_model.md', 'case_model_schema.json']
    return (protocol_hash() + ''.join(digest((ROOT / f).read_text()) for f in files)
            + json.dumps(settings, sort_keys=True) + arm)


def run_arm(arm, notes, engine, config, model_name, fields, common):
    settings = {**config['generation'], 'engine': config['engine']}
    directory = arm_directory(config, arm, model_name, config['generation'])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / '.run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_locked(arm, notes, engine, config, model_name, fields, common, settings, directory)


def run_locked(arm, notes, engine, config, model_name, fields, common, settings, directory):
    identity = code_identity(settings, arm)
    cases = [Case(row, directory, identity, model_name) for row in notes]
    todo = [c for c in cases if not c.finished()]
    started = now()
    # Chunks of cases: each chunk writes its results, so a job stopped by its time limit resumes there.
    size = config.get('chunk_size') or len(todo) or 1
    for start in range(0, len(todo), size):
        chunk = todo[start:start + size]
        for case in chunk:
            prepare_case_dirs(case)
        for case, result in RUNNERS[arm](chunk, engine, config['generation'], model_name, fields, common):
            atomic_json(case.result_path, result)
            print(json.dumps({'arm': arm, 'patient_id': case.pid, 'valid': result['valid'],
                              'valid_fields': result['valid_fields'], 'calls': result['new_calls'],
                              'errors': len(result['errors'])}), flush=True)
    print(json.dumps({'arm': arm, 'cases': len(todo), 'skipped_finished': len(cases) - len(todo),
                      'wall_s': round((now() - started).total_seconds(), 1)}), flush=True)


def equivalence_check(engine, notes, config, fields, common, out_path):
    """Batch invariance on real prompts: first-attempt group prompts of each case, in one batch vs alone."""
    requests = [(render_prompt(common, g), evidence_index(r['text'])[0], config['generation']['max_new_tokens'])
                for r in notes for g in groups(fields)]
    batched = [raw for raw, _ in engine.generate(requests)]
    alone = [engine.generate([req])[0][0] for req in requests]
    report = {'pipeline': PIPELINE_VERSION, 'engine': engine.setup, 'requests': len(requests),
              'identical': sum(a == b for a, b in zip(batched, alone)),
              'differing_units': [i for i, (a, b) in enumerate(zip(batched, alone)) if a != b]}
    atomic_json(out_path, report)
    print(json.dumps({k: v for k, v in report.items() if k != 'engine'}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=ROOT / 'config_vllm.json')
    parser.add_argument('--input-dir', help='Override only for offline dataset verification')
    parser.add_argument('--model', choices=['Gemma4', 'Llama-70B', 'Qwen'])
    parser.add_argument('--arm', choices=list(ARMS) + ['all'], default='all')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--case-id', action='append', help='Repeat to select several cases explicitly')
    parser.add_argument('--equivalence', action='store_true', help='Batch-vs-alone check on group prompts')
    parser.add_argument('--run', action='store_true', help='Load the model; requires SLURM')
    parser.add_argument('--check', action='store_true', help='Verify bundle and input without inference')
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(args.config.read_text())
    if args.model:  # model-specific engine settings (e.g. one GPU for a smaller model)
        config['engine'] = {**config['engine'], **config.get('engine_overrides', {}).get(args.model, {})}
    fields, common = bundle()
    if json.loads((ROOT / 'manifest.json').read_text())['protocol_sha256'] != protocol_hash():
        raise ValueError('Prompts out of date: regenerate with protocol.py')
    for group in groups(fields):
        if (ROOT / 'prompts' / (group['name'] + '.md')).read_text() != render_prompt(common, group):
            raise ValueError('Generated prompt differs from protocol')
    json.loads(CASE_MODEL_SCHEMA.read_text())
    rows = read_inputs(config, args.input_dir)
    if args.case_id:
        notes = [select_notes(rows, 0, cid)[0] for cid in args.case_id]
    else:
        notes = select_notes(rows, args.limit)
    arms = list(ARMS) if args.arm == 'all' else [args.arm]
    print(json.dumps({'pipeline': PIPELINE_VERSION, 'protocol': VERSION, 'arms': arms, 'cases': len(notes),
                      'engine': config['engine'], 'inference_requested': args.run}), flush=True)
    if not args.run or args.check:
        return
    if not os.environ.get('SLURM_JOB_ID'):
        raise RuntimeError('Inference must run inside a SLURM allocation')
    if args.input_dir:
        raise ValueError('Input override is check-only; version a config to change inference data')
    from engine_vllm import Engine
    engine = Engine(args.model, config['engine'])
    print(json.dumps({'engine_setup': engine.setup}), flush=True)
    if args.equivalence:
        equivalence_check(engine, notes, config, fields, common,
                          Path(config['output_dir']) / 'equivalence' / f'{args.model}.json')
        return
    for arm in arms:
        run_arm(arm, notes, engine, config, args.model, fields, common)


if __name__ == '__main__':
    main()
