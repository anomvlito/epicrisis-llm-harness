#!/usr/bin/env python3
"""Post-hoc sensitivity S1 (declared after the primary run): informative feedback after a parser rejection.

In the primary run (form199-v2.2) a response rejected by the strict parser was retried with the generic
message 'root: expected exactly one JSON object (strict parser)', and some units returned the same response
on every retry. S1 asks what each arm yields when that message also names the cause: a duplicate key, a
response cut before the object closed, text after the object, or invalid JSON at a position.

Each unit replays the primary run exactly up to its first rejected attempt: those attempts are reused from the
primary traces, the rejected output is fed back with the named cause, and only the remaining attempts are
generated. Units without a rejection, or rejected only at their last allowed attempt, would follow the same
path in both conditions and are reused verbatim. In Arm C a case model whose final output changes also
regenerates the 18 group calls of that case, because their context changes. Replaying instead of regenerating
from the first attempt keeps batch-composition noise out of the comparison.
Prompts, validation, retry limit, engine and code are those of the primary run (checked through the
primary result signatures). Results go to a separate directory; primary files are never modified.

    python3 sensitivity_feedback.py --model Qwen --arm all --limit 50 --plan   # counts only, no GPU
    python3 sensitivity_feedback.py --model Qwen --arm all --limit 50 --run    # inside SLURM
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from protocol import FENCE_OPEN, ROOT, VERSION, bundle, groups, digest, validate
from runner import atomic_json, read_inputs, select_notes
from monolithic import render_monolithic_prompt, field_level_valid
from arm_c import validate_case_model, case_model_block, CASE_MODEL_PROMPT, ARMC_VERSION
from batch_driver import (ARMS, Case, Unit, run_rounds, prepare_case_dirs, group_units, base_result,
                          arm_directory, code_identity)

S1_VERSION = 'form199-v2.2-s1'
ROOT_ERROR = 'root: expected exactly one JSON object'
STAMP = r'_\d{8}T\d{12}\.json'


class DuplicateKey(ValueError):
    def __init__(self, key):
        super().__init__(key)
        self.key = key


def rejection_reason(raw):
    """Why the strict parser rejected a response, in the language of the prompts."""
    if not isinstance(raw, str) or not raw.strip():
        return 'la respuesta está vacía'
    text, fenced = raw, FENCE_OPEN.match(raw)
    if fenced:
        text = text[fenced.end():]
    text = text.lstrip()

    def unique(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen:
                raise DuplicateKey(key)
            seen.add(key)
        return dict(pairs)

    def no_constant(name):
        raise ValueError('non-finite')
    try:
        value, end = json.JSONDecoder(object_pairs_hook=unique, parse_constant=no_constant).raw_decode(text)
    except DuplicateKey as error:
        return (f'la clave "{error.key}" aparece más de una vez en un mismo objeto; cada clave debe aparecer '
                'una sola vez, así que combina esas entradas en una')
    except json.JSONDecodeError as error:
        if error.msg.startswith('Unterminated string') or error.pos >= len(text.rstrip()) - 1:
            return ('la respuesta se cortó antes de cerrar el objeto JSON (probablemente alcanzó el límite de '
                    'tokens); escribe textos más breves y no repitas contenido')
        return f'JSON inválido ({error.msg}, carácter {error.pos})'
    except ValueError:
        return 'hay un número no finito (NaN o Infinity)'
    rest = text[end:].strip()
    if fenced and rest.startswith('```'):
        rest = rest[3:].strip()
    if rest:
        return 'hay texto después del objeto JSON; devuelve solo el objeto'
    if not isinstance(value, dict):
        return 'la raíz no es un objeto JSON'
    return 'motivo no identificado'


def informative(check):
    """Same validation; a rejected response (kept as raw text by strict_output) also gets its cause."""
    def wrapped(output):
        errors = check(output)
        if isinstance(output, str):
            reason = rejection_reason(output)
            errors = [e + '; motivo: ' + reason if e.startswith(ROOT_ERROR) else e for e in errors]
        return errors
    return wrapped


def primary_traces(directory, name):
    """(paths, traces) of one unit in the primary run, ordered by attempt; attempts must be 1..n."""
    if not directory.exists():
        return [], []
    rx = re.compile(re.escape(name) + STAMP)
    items = sorted(((p, json.loads(p.read_text())) for p in directory.iterdir() if rx.fullmatch(p.name)),
                   key=lambda item: item[1]['attempt'])
    if [t['attempt'] for _, t in items] != list(range(1, len(items) + 1)):
        raise ValueError(f'Incomplete primary traces for {directory}/{name}')
    return [p for p, _ in items], [t for _, t in items]


class Plan:
    """What happens to each unit of one case: reused from the primary run, replayed from its first
    rejection with informative feedback, or new (no primary counterpart because its context changed)."""
    def __init__(self, max_retries):
        self.max_retries = max_retries
        self.reused, self.rerun, self.new, self.copies = [], [], [], []
        self.copies_by_name, self.primary_final, self.replayed_from = {}, {}, {}

    def reuse(self, unit, paths, traces):
        last = traces[-1]
        unit.attempts, unit.output, unit.errors = len(traces), last['output'], last['errors']
        unit.done, unit.valid = True, not last['errors']
        self.reused.append(unit.name)
        self.copies += paths
        return unit

    def fresh(self, unit):
        unit.check = informative(unit.check)
        self.new.append(unit.name)
        return unit

    def replay(self, unit, paths, traces, k):
        # Attempts 1..k come from the primary run; attempt k was rejected and is answered with its cause.
        if unit.prompt != traces[0]['prompt']:
            raise ValueError(f'Prompt of {unit.case.pid}/{unit.name} differs from the primary run')
        unit.check = informative(unit.check)
        unit.attempts, unit.output = k, traces[k - 1]['output']
        unit.errors = unit.check(unit.output)
        self.rerun.append(unit.name)
        self.replayed_from[unit.name] = k
        self.copies += paths[:k]
        return unit

    def decide(self, unit, paths, traces):
        if not traces:
            return self.fresh(unit)
        self.primary_final[unit.name] = traces[-1]['output']
        first = next((i for i, t in enumerate(traces, 1)
                      if (t.get('inference') or {}).get('parse_mode') == 'rejected'), None)
        if first is None or first > self.max_retries:
            return self.reuse(unit, paths, traces)
        return self.replay(unit, paths, traces, first)


def group_traces_dir(directory, pid):
    return directory / 'checkpoints' / pid / 'traces'


def copy_reused(plan, pdir, sdir):
    for path in plan.copies:
        target = sdir / path.relative_to(pdir)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(path, target)


def write_group_checkpoints(case, units):
    for unit in units:
        if unit.name in case.plan.reused:
            last = json.loads(case.plan.copies_by_name[unit.name][-1].read_text())
            atomic_json(case.directory / 'checkpoints' / case.pid / f'{unit.name}.json', last)


class S1Case(Case):
    def __init__(self, row, sdir, identity, model_name, pdir, primary_identity):
        super().__init__(row, sdir, identity, model_name)
        self.primary_path = pdir / 'results' / (self.pid + '.json')
        self.primary = json.loads(self.primary_path.read_text())
        expected = Case(row, pdir, primary_identity, model_name).signature
        if self.primary.get('signature') != expected:
            raise ValueError(f'{self.pid}: primary result was produced by different code or settings')
        self.plan = None


def decide_group(case, unit, pdir, force_new=False):
    paths, traces = primary_traces(group_traces_dir(pdir, case.pid), unit.name)
    if force_new:
        paths, traces = [], []
    case.plan.copies_by_name[unit.name] = paths
    return case.plan.decide(unit, paths, traces)


def assemble_groups(case, units, fields, model_name, strategy, setup, settings):
    merged = {}
    for unit in units:
        if unit.valid:
            merged.update(unit.output)
    errors = validate(merged, fields, case.index)
    return base_result(case, model_name, strategy, setup, settings, errors, merged, sum(u.attempts for u in units))


def s1_harness(cases, engine, settings, model_name, fields, common, pdir):
    by_group = groups(fields)
    units = {c.pid: [decide_group(c, u, pdir) for u in group_units(c, common, by_group, settings['max_new_tokens'])]
             for c in cases}
    engine.run([u for us in units.values() for u in us], settings['max_retries'])
    for case in cases:
        write_group_checkpoints(case, units[case.pid])
        yield case, assemble_groups(case, units[case.pid], fields, model_name, 'harness', engine.setup_for(case),
                                    settings)


def s1_monolithic(cases, engine, settings, model_name, fields, common, pdir):
    prompt = render_monolithic_prompt(common, fields)
    units = {}
    for case in cases:
        unit = Unit(case, 'form', 'form', prompt, settings['monolithic_max_new_tokens'],
                    lambda out, c=case: validate(out, fields, c.index))
        paths, traces = primary_traces(pdir / 'traces' / case.pid, 'form')
        units[case.pid] = case.plan.decide(unit, paths, traces)
    engine.run(list(units.values()), settings['max_retries'])
    for case in cases:
        unit = units[case.pid]
        kept = {k: unit.output[k] for k in field_level_valid(unit.output, fields, case.index)}
        yield case, base_result(case, model_name, 'monolithic', engine.setup_for(case), settings, unit.errors,
                                kept, unit.attempts)


def s1_armc(cases, engine, settings, model_name, fields, common, pdir):
    instruction = common + '\n\n' + CASE_MODEL_PROMPT.read_text()
    cm_units = {}
    for case in cases:
        unit = Unit(case, 'case_model', 'case_model', instruction, settings['case_model_max_new_tokens'],
                    lambda out, c=case: validate_case_model(out, c.index))
        paths, traces = primary_traces(pdir / 'case_models' / 'traces' / case.pid, 'case_model')
        cm_units[case.pid] = case.plan.decide(unit, paths, traces)
    engine.run(list(cm_units.values()), settings['max_retries'])
    ready, context, units = [], {}, {}
    for case in cases:
        unit = cm_units[case.pid]
        atomic_json(case.directory / 'case_models' / (case.pid + '.json'),
                    {'signature': case.signature, 'case_model': unit.output, 'errors': unit.errors,
                     'calls': unit.attempts})
        if unit.valid:
            ready.append(case)
            context[case.pid] = case_model_block(unit.output)
        else:
            result = base_result(case, model_name, 'armc', engine.setup_for(case), settings,
                                 ['case_model: ' + e for e in unit.errors], {}, unit.attempts)
            yield case, {**result, 'strategy_version': ARMC_VERSION, 'case_model_valid': False}
    by_group = groups(fields)
    for case in ready:
        # A case model whose final output changed changes the context of every group of the case.
        changed = ('case_model' in case.plan.rerun
                   and cm_units[case.pid].output != case.plan.primary_final['case_model'])
        units[case.pid] = [decide_group(case, u, pdir, force_new=changed) for u in
                           group_units(case, common, by_group, settings['max_new_tokens'], context[case.pid])]
    engine.run([u for us in units.values() for u in us], settings['max_retries'])
    for case in ready:
        write_group_checkpoints(case, units[case.pid])
        result = assemble_groups(case, units[case.pid], fields, model_name, 'armc', engine.setup_for(case), settings)
        calls = cm_units[case.pid].attempts
        yield case, {**result, 'new_calls': result['new_calls'] + calls, 'strategy_version': ARMC_VERSION,
                     'case_model_valid': True, 'case_model_calls': calls}


S1_RUNNERS = {'harness': s1_harness, 'monolithic': s1_monolithic, 'armc': s1_armc}
COMPARED = ('valid', 'errors', 'valid_fields', 'new_calls', 'annotations')


class LazyEngine:
    """Loads the model only if some unit must be generated; reused-only cases keep the primary engine record."""
    def __init__(self, factory, plan_only):
        self.factory, self.plan_only, self.engine, self.generated = factory, plan_only, None, set()

    def run(self, units, max_retries):
        pending = [u for u in units if not u.done]
        if not pending:
            return
        if self.plan_only:
            for u in pending:  # counts only: mark as run so later phases can be planned
                u.done, u.valid, u.output, u.errors = True, u.kind == 'case_model', {}, []
                self.generated.add(u.case.pid)
            return
        if self.engine is None:
            self.engine = self.factory()
            print(json.dumps({'engine_setup': self.engine.setup}), flush=True)
        run_rounds(units, self.engine, max_retries)
        self.generated.update(u.case.pid for u in pending)

    def setup_for(self, case):
        return self.engine.setup if case.pid in self.generated and self.engine else case.primary['engine']


def run_arm_s1(arm, notes, factory, config, model_name, fields, common, primary_root, s1_root, plan_only):
    settings = {**config['generation'], 'engine': config['engine']}
    pdir = arm_directory({'output_dir': primary_root}, arm, model_name, config['generation'])
    sdir = arm_directory({'output_dir': s1_root}, arm, model_name, config['generation'])
    primary_identity = code_identity(settings, arm)
    identity = primary_identity + digest(Path(__file__).read_text()) + 'S1'
    available = [r for r in notes if (pdir / 'results' / (r['patient_id'] + '.json')).exists()]
    sdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (sdir / '.run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cases = [S1Case(r, sdir, identity, model_name, pdir, primary_identity) for r in available]
        for case in cases:
            case.plan = Plan(settings['max_retries'])
        todo = [c for c in cases if plan_only or not c.finished()]
        if not plan_only:
            for case in todo:
                prepare_case_dirs(case)
        engine = LazyEngine(factory, plan_only)
        summary = {'arm': arm, 'model': model_name, 'primary_results': len(available), 'missing_primary':
                   len(notes) - len(available), 'already_done': len(cases) - len(todo), 'cases_regenerated': 0,
                   'units_reused': 0, 'units_rerun': 0, 'units_new': 0, 'reused_cases_identical': 0,
                   'changed_valid': 0}
        for case, result in S1_RUNNERS[arm](todo, engine, settings, model_name, fields, common, pdir):
            plan = case.plan
            summary['units_reused'] += len(plan.reused)
            summary['units_rerun'] += len(plan.rerun)
            summary['units_new'] += len(plan.new)
            if plan_only:
                summary['cases_regenerated'] += bool(plan.rerun or plan.new)
                continue
            regenerated = bool(plan.rerun or plan.new)
            if not regenerated:
                differing = [k for k in COMPARED if result[k] != case.primary[k]]
                if differing:
                    raise ValueError(f'{case.pid}: reused units do not reproduce the primary result {differing}')
                summary['reused_cases_identical'] += 1
            summary['cases_regenerated'] += regenerated
            summary['changed_valid'] += result['valid'] != case.primary['valid']
            copy_reused(plan, pdir, sdir)
            atomic_json(case.result_path, {
                **result, 'pipeline': S1_VERSION, 'sensitivity': 'S1_informative_parse_feedback',
                'primary_signature': case.primary['signature'], 'primary_valid': case.primary['valid'],
                'units_reused': plan.reused, 'units_rerun': plan.rerun, 'units_new': plan.new,
                'replayed_from_attempt': plan.replayed_from})
            if regenerated:
                print(json.dumps({'arm': arm, 'patient_id': case.pid, 'valid': result['valid'],
                                  'primary_valid': case.primary['valid'], 'valid_fields': result['valid_fields'],
                                  'rerun': plan.rerun, 'from_attempt': plan.replayed_from,
                                  'new': len(plan.new)}), flush=True)
        print(json.dumps(summary), flush=True)
        return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=ROOT / 'config_vllm.json')
    parser.add_argument('--model', choices=['Gemma4', 'Llama-70B', 'Qwen'], required=True)
    parser.add_argument('--arm', choices=list(ARMS) + ['all'], default='all')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--output-dir', help='Default: primary output_dir + "-s1"')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true', help='Count units to reuse and regenerate; no model')
    mode.add_argument('--run', action='store_true', help='Load the model if needed; requires SLURM')
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(args.config.read_text())
    config['engine'] = {**config['engine'], **config.get('engine_overrides', {}).get(args.model, {})}
    fields, common = bundle()
    notes = select_notes(read_inputs(config), args.limit)
    primary_root = config['output_dir']
    s1_root = args.output_dir or primary_root.rstrip('/') + '-s1'
    if args.plan:  # planning writes nothing next to the real results
        s1_root = tempfile.mkdtemp(prefix='s1-plan-')
    if args.run and not os.environ.get('SLURM_JOB_ID'):
        raise RuntimeError('Inference must run inside a SLURM allocation')

    def factory():
        from engine_vllm import Engine
        return Engine(args.model, config['engine'])
    print(json.dumps({'pipeline': S1_VERSION, 'protocol': VERSION, 'primary': primary_root, 'output': s1_root,
                      'model': args.model, 'plan_only': args.plan}), flush=True)
    for arm in (list(ARMS) if args.arm == 'all' else [args.arm]):
        run_arm_s1(arm, notes, factory, config, args.model, fields, common, primary_root, s1_root, args.plan)


if __name__ == '__main__':
    main()
