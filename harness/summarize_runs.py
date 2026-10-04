#!/usr/bin/env python3
"""Aggregate structural and operational metrics of form199-v1 runs. No clinical text is printed.

    python3 summarize_runs.py --input-dir <cohort input> --run harness=<dir> --run monolithic=<dir>

<dir> is one model directory: <output_dir>/<model> for the harness, or
<output_dir>-monolithic/<model>-b<budget> for the monolithic arm. The output is a JSON with
counts and summaries per case and per run; patient identifiers are kept because they are the
pseudonymous join key, but no note text, evidence text or model output is included.
"""
from __future__ import annotations
import argparse
import collections
import json
import re
from pathlib import Path
from protocol import bundle, digest, parse_model_output, validate, evidence_index

# Order matters: the first matching pattern names the error category.
ERROR_PATTERNS = [
    ('root_not_object', 'root: expected'),
    ('missing_field', 'missing field'),
    ('unexpected_field', 'unexpected field'),
    ('entry_not_object', 'expected response object'),
    ('entry_properties', 'response properties must match'),
    ('evidence_id_invalid', 'evidence_ids must reference'),
    ('evidence_id_repeated', 'repeated evidence ID'),
    ('evidence_missing', 'requires evidence'),
    ('doubt_without_level', 'doubt requires an application uncertainty level'),
    ('uncertainty_misplaced', 'incertidumbre only applies'),
    ('boolean_value', 'boolean requires true, false'),
    ('boolean_missingness', 'boolean cannot have motivo_nulo'),
    ('null_without_reason', 'null requires explicit missingness'),
    ('quality_missing', 'document quality must be assessed'),
    ('present_with_reason', 'present value cannot have motivo_nulo'),
    ('choice_invalid', 'value outside application choices'),
    ('date_invalid', 'expected valid DD/MM/AAAA'),
    ('text_invalid', 'expected nonempty text'),
    ('comment_type', 'comentario must be'),
    ('relation_parent_child', 'requires parent'),
    ('relation_negative_parent', 'No parent'),
    ('relation_exclusion', 'unresolved application exclusion'),
    ('relation_deceased_destination', 'deceased requires'),
]


def categorise(errors):
    counts = collections.Counter()
    for e in errors:
        counts[next((name for name, pattern in ERROR_PATTERNS if pattern in e), 'other')] += 1
    return dict(counts)


def strict_json(raw):
    if not isinstance(raw, str):
        return None
    try:
        json.loads(raw.strip())
        return True
    except json.JSONDecodeError:
        return False


def describe(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    return {'n': len(values), 'sum': round(sum(values), 2), 'median': round(median, 2),
            'min': round(values[0], 2), 'max': round(values[-1], 2)}


def normalize_missingness(annotations, fields):
    """Sensitivity analysis only: a non-boolean answer that is null because it was "not documented"
    is relabelled "not applicable" when a boolean ancestor is No, or when it is the discharge
    destination of a deceased patient. Values are never changed; outputs on disk are untouched."""
    by_key = {f['key']: f for f in fields}
    value = lambda k: (annotations.get(k) or {}).get('valor')
    out, changed = {}, 0
    for key, entry in annotations.items():
        entry = {k: v for k, v in entry.items() if k != 'evidencias'}
        field = by_key.get(key)
        if field and field['type'] != 'leaf' and entry.get('valor') is None \
                and entry.get('motivo_nulo') == 'no_documentado':
            negative_parent = any(a['type'] == 'leaf' and value(a['key']) is False for a in field['ancestors'])
            deceased = key == 'egreso.destino' and value('egreso.estado_vital') == 'Fallecido'
            if negative_parent or deceased:
                entry = {**entry, 'motivo_nulo': 'no_aplica'}
                changed += 1
        out[key] = entry
    return out, changed


def trace_files(run_dir, strategy, pid):
    # harness and armc keep group traces under checkpoints/<pid>/traces; armc adds its case-model call.
    if strategy in ('harness', 'armc'):
        folders = [run_dir / 'checkpoints' / pid / 'traces']
        if strategy == 'armc':
            folders.append(run_dir / 'case_models' / 'traces' / pid)
    else:
        folders = [run_dir / 'traces' / pid]
    return sorted(f for folder in folders if folder.exists() for f in folder.glob('*.json'))


def summarise_case(result, traces, note, fields):
    leaf = {f['key'] for f in fields if f['type'] == 'leaf'}
    ann = result['annotations']
    values = collections.Counter()
    missing_reasons = collections.Counter()
    evidence_ids = offsets_ok = 0
    for key, entry in ann.items():
        v = entry['valor']
        if key in leaf:
            values['true' if v is True else 'false' if v is False else 'unknown' if v == 'unknown' else 'other'] += 1
        elif v is None:
            missing_reasons[entry.get('motivo_nulo')] += 1
        else:
            values['nonboolean_present'] += 1
        for ev in entry.get('evidencias', []):
            evidence_ids += 1
            offsets_ok += note[ev['start']:ev['end']] == ev['text']
    attempts = []
    for path in traces:
        t = json.loads(path.read_text())
        meta = t.get('inference') or {}
        gpu = meta.get('gpu_summary') or {}
        attempts.append({
            'unit': t.get('group', 'case_model' if 'case_model' in path.name else 'form'), 'attempt': t['attempt'], 'valid': not t['errors'],
            'errors': categorise(t['errors']), 'n_errors': len(t['errors']),
            'finish_reason': meta.get('finish_reason'), 'truncated': meta.get('truncated'),
            'tokens_in': meta.get('tokens_entrada'), 'tokens_out': meta.get('tokens_salida'),
            'latency_s': meta.get('latencia_s'), 'tokens_per_s': meta.get('tokens_por_segundo'),
            'strict_json': strict_json(meta.get('raw_response')),
            # v1.1 traces carry the mode used online; v1 traces are re-parsed offline.
            'parse_mode': meta.get('parse_mode') or parse_model_output(meta.get('raw_response'))[1],
            'legacy_parser_object': meta.get('legacy_parser_object'),
            # v1.5: bracketed evidence IDs rewritten before validation (absent in earlier traces).
            'evidence_ids_unbracketed': meta.get('evidence_ids_unbracketed') or 0,
            # v1.9: boolean labels normalized before validation (absent in earlier traces).
            'boolean_labels_normalized': meta.get('boolean_labels_normalized') or 0,
            'gpu_used_gb_max': sum(d.get('used_gb_max', 0) for d in gpu.values()) or None,
        })
    normalized, relabelled = normalize_missingness(ann, fields)
    errors_normalized = validate(normalized, fields, evidence_index(note)[1]) if len(normalized) == len(fields) else None
    units = collections.defaultdict(list)
    for a in attempts:
        units[a['unit']].append(a)
    unit_outcome = collections.Counter()
    for unit, rows in units.items():
        rows.sort(key=lambda r: r['attempt'])
        if rows[0]['valid']:
            unit_outcome['valid_first_attempt'] += 1
        elif any(r['valid'] for r in rows):
            unit_outcome['recovered_after_retry'] += 1
        else:
            unit_outcome['failed'] += 1
    first = [min(rows, key=lambda r: r['attempt']) for rows in units.values()]
    return {
        'patient_id': result['patient_id'], 'valid': result['valid'],
        'valid_fields': result['valid_fields'], 'missing_fields': len(fields) - result['valid_fields'],
        'final_errors': categorise(result['errors']), 'n_final_errors': len(result['errors']),
        'sensitivity_missingness_relabelled': relabelled,
        'sensitivity_valid_after_relabelling': (not errors_normalized) if errors_normalized is not None else False,
        'sensitivity_errors_after_relabelling': categorise(errors_normalized or []),
        'calls': len(attempts), 'units': len(units), 'unit_outcomes': dict(unit_outcome),
        'first_attempt_units_valid': sum(r['valid'] for r in first),
        'first_attempt_errors': dict(sum((collections.Counter(r['errors']) for r in first), collections.Counter())),
        'finish_reasons': dict(collections.Counter(a['finish_reason'] for a in attempts)),
        'truncated_calls': sum(bool(a['truncated']) for a in attempts),
        'strict_json_calls': sum(a['strict_json'] is True for a in attempts),
        'parse_modes': dict(collections.Counter(a['parse_mode'] for a in attempts)),
        'evidence_ids_unbracketed': sum(a['evidence_ids_unbracketed'] for a in attempts),
        'boolean_labels_normalized': sum(a['boolean_labels_normalized'] for a in attempts),
        'tokens_in': sum(a['tokens_in'] or 0 for a in attempts),
        'tokens_out': sum(a['tokens_out'] or 0 for a in attempts),
        'latency_s': round(sum(a['latency_s'] or 0 for a in attempts), 1),
        'tokens_per_s': describe([a['tokens_per_s'] for a in attempts]),
        'gpu_used_gb_max': max((a['gpu_used_gb_max'] or 0 for a in attempts), default=None),
        'tokens_in_per_call': describe([a['tokens_in'] for a in attempts]),
        'tokens_out_per_call': describe([a['tokens_out'] for a in attempts]),
        'boolean_values': dict(values), 'nonboolean_null_reasons': dict(missing_reasons),
        'evidence_items': evidence_ids, 'evidence_offsets_match': offsets_ok,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input-dir', type=Path, required=True)
    parser.add_argument('--run', action='append', required=True, help='strategy=directory')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    fields, _ = bundle()
    notes = {json.loads(l)['patient_id']: json.loads(l)['text']
             for l in (args.input_dir / 'notes.jsonl').read_text().splitlines() if l.strip()}
    report = []
    for spec in args.run:
        strategy, directory = spec.split('=', 1)
        run_dir = Path(directory)
        cases = []
        for path in sorted((run_dir / 'results').glob('*.json')):
            result = json.loads(path.read_text())
            note = notes[result['patient_id']]
            if result['note_sha256'] != digest(note):
                raise ValueError('Result does not match the frozen note')
            cases.append(summarise_case(result, trace_files(run_dir, strategy, result['patient_id']), note, fields))
        report.append({'strategy': strategy, 'run_dir': str(run_dir), 'cases': len(cases),
                       'valid_cases': sum(c['valid'] for c in cases), 'per_case': cases})
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
