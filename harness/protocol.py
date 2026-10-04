"""Versioned 199-variable annotation protocol. Standard library only."""
from __future__ import annotations
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION = 'form199-v1.9'
RESPONSE_KEYS = {'valor', 'evidence_ids', 'incertidumbre', 'comentario', 'motivo_nulo'}
SUSPICION = ('Alto', 'Bajo', 'Indeterminado')
SUBJECTIVE = {'calidad.global', 'calidad.comentario'}
# v1.1: the only tolerated wrapper is one markdown fence around a single JSON object.
FENCE_OPEN = re.compile(r'\A\s*```(?:json)?[ \t]*\r?\n')


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def parse_model_output(raw):
    """Strict parse of a raw model response: exactly one JSON object, optionally inside one
    markdown fence (the closing fence may be absent because generation stops at the object's
    end). Prose, extra objects, duplicate keys, NaN/Infinity or truncation are rejected; nothing
    is salvaged from surrounding text. Returns (object or None, 'bare' | 'fenced' | 'rejected')."""
    if not isinstance(raw, str):
        return None, 'rejected'
    mode, text = 'bare', raw
    fence = FENCE_OPEN.match(text)
    if fence:
        mode, text = 'fenced', text[fence.end():]
    text = text.lstrip()
    def unique(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError('duplicate key')
        return dict(pairs)
    def no_constant(name):
        raise ValueError('non-finite number')
    decoder = json.JSONDecoder(object_pairs_hook=unique, parse_constant=no_constant)
    try:
        value, end = decoder.raw_decode(text)
    except ValueError:
        return None, 'rejected'
    rest = text[end:].strip()
    if mode == 'fenced' and rest.startswith('```'):
        rest = rest[3:].strip()
    if rest or not isinstance(value, dict):
        return None, 'rejected'
    return value, mode


# v1.5: the note shows each line as "[E0001] text", so "[E0001]" in evidence_ids names the same line.
BRACKETED_ID = re.compile(r'\A\s*\[(E\d{4,})\]\s*\Z')


def unbracket_evidence_ids(value):
    """Rewrite "[E0001]" as "E0001" in every evidence_ids list, at any depth. Returns the count."""
    count = 0
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'evidence_ids' and isinstance(item, list):
                for i, ident in enumerate(item):
                    match = BRACKETED_ID.match(ident) if isinstance(ident, str) else None
                    if match:
                        item[i] = match.group(1)
                        count += 1
            else:
                count += unbracket_evidence_ids(item)
    elif isinstance(value, list):
        count += sum(unbracket_evidence_ids(item) for item in value)
    return count


# v1.9: the manual reads a boolean that "does not apply" or "is not mentioned" as No. A boolean answered
# null *with* an explicit reason is therefore No, and a reason attached to a boolean No is dropped. A null
# without a reason stays unanswered and fails validation; nothing else is touched.
BOOLEAN_NULL_REASONS = ('no_aplica', 'no_documentado')


def normalize_boolean_labels(output, types):
    """Rewrite boolean (leaf) entries in place; `types` maps key -> field type. Returns the count."""
    count = 0
    if not isinstance(output, dict):
        return 0
    for key, entry in output.items():
        if types.get(key) != 'leaf' or not isinstance(entry, dict):
            continue
        if entry.get('valor') is None and entry.get('motivo_nulo') in BOOLEAN_NULL_REASONS:
            entry['valor'], entry['motivo_nulo'] = False, None
            count += 1
        elif entry.get('valor') is False and entry.get('motivo_nulo') is not None:
            entry['motivo_nulo'] = None
            count += 1
    return count


def strict_output(meta):
    # Parsed object, or the raw text when rejected so a retry can show the model what it wrote;
    # validate() rejects anything that is not an object. The raw response is kept unchanged.
    value, mode = parse_model_output(meta.get('raw_response'))
    meta['parse_mode'] = mode
    meta['evidence_ids_unbracketed'] = unbracket_evidence_ids(value) if mode != 'rejected' else 0
    return (value if mode != 'rejected' else meta.get('raw_response', '')), meta


def catalog(schema):
    result = []
    def visit(nodes, ancestors):
        for node in nodes:
            descriptor = {k: v for k, v in node.items() if k != 'children'}
            if node['type'] != 'mother':
                descriptor['ancestors'] = [
                    {k: a[k] for k in ('key', 'label', 'type')} for a in ancestors
                ]
                result.append(descriptor)
            visit(node.get('children', []), ancestors + [node])
    visit(schema, [])
    keys = [x['key'] for x in result]
    if len(keys) != 199 or len(set(keys)) != 199:
        raise ValueError('Expected exactly 199 unique application variables')
    return result


def bundle():
    schema = json.loads((ROOT / 'form_schema.json').read_text())
    fields = catalog(schema)
    common = (ROOT / 'prompts/common.md').read_text()
    return fields, common


def groups(fields, size=16):
    result = []
    for block in dict.fromkeys(f['key'].split('.')[0] for f in fields):
        block_fields = [f for f in fields if f['key'].split('.')[0] == block]
        for start in range(0, len(block_fields), size):
            result.append({'name': f'{block}_{start // size + 1:02}',
                           'fields': block_fields[start:start + size]})
    return result


def render_prompt(common, group):
    return (common + '\n\n## Grupo ' + group['name']
            + '\nLas claves exactas y las definiciones de este grupo son:\n'
            + json.dumps(group['fields'], ensure_ascii=False, indent=2)
            + '\n\nLa división en grupos es solo técnica. Lee toda la epicrisis; '
            'cada clave conserva su contexto y sus relaciones del catálogo.\n')


def evidence_index(note):
    # Keep offsets and original line numbers, including gaps caused by blank lines.
    index = {}
    offset = 0
    for i, raw in enumerate(note.splitlines(keepends=True), 1):
        text = raw.rstrip('\r\n')
        if text.strip():
            index[f'E{i:04}'] = {'text': text, 'start': offset, 'end': offset + len(text)}
        offset += len(raw)
    return '\n'.join(f'[{k}] {v["text"]}' for k, v in index.items()), index


def validate(result, fields, index, *, relations=True):
    errors = []
    if not isinstance(result, dict):
        return ['root: expected exactly one JSON object (strict parser)']
    expected = {f['key'] for f in fields}
    errors += [f'{k}: missing field' for k in sorted(expected - result.keys())]
    errors += [f'{k}: unexpected field' for k in sorted(result.keys() - expected)]
    for field in fields:
        key = field['key']
        entry = result.get(key)
        if not isinstance(entry, dict):
            if key in result:
                errors.append(f'{key}: expected response object')
            continue
        if set(entry) != RESPONSE_KEYS:
            errors.append(f'{key}: response properties must match protocol exactly')
            continue
        value, ids = entry['valor'], entry['evidence_ids']
        valid_ids = (isinstance(ids, list) and all(isinstance(i, str) and i in index for i in ids))
        if not valid_ids:
            errors.append(f'{key}: evidence_ids must reference existing lines')
        elif len(ids) != len(set(ids)):
            errors.append(f'{key}: repeated evidence ID')
        if entry['comentario'] is not None and not isinstance(entry['comentario'], str):
            errors.append(f'{key}: comentario must be string or null')
        doubt = field['type'] == 'leaf' and value == 'unknown'
        if doubt:
            if entry['incertidumbre'] not in SUSPICION:
                errors.append(f'{key}: doubt requires an application uncertainty level')
        elif entry['incertidumbre'] is not None:
            errors.append(f'{key}: incertidumbre only applies to doubt')
        if field['type'] == 'leaf':
            if not (type(value) is bool or value == 'unknown'):
                errors.append(f'{key}: boolean requires true, false or "unknown"; null is unanswered')
            if entry['motivo_nulo'] is not None:
                errors.append(f'{key}: boolean cannot have motivo_nulo')
            if (value is True or doubt) and (not valid_ids or not ids):
                errors.append(f'{key}: yes/doubt requires evidence')
        elif value is None:
            allowed = {'no_documentado', 'no_aplica'}
            if key == 'calidad.comentario':
                allowed.add('opcional')
            if not isinstance(entry['motivo_nulo'], str) or entry['motivo_nulo'] not in allowed:
                errors.append(f'{key}: null requires explicit missingness reason')
            if key == 'calidad.global':
                errors.append(f'{key}: document quality must be assessed')
        else:
            if entry['motivo_nulo'] is not None:
                errors.append(f'{key}: present value cannot have motivo_nulo')
            if field['type'] == 'select' and value not in field['choices']:
                errors.append(f'{key}: value outside application choices')
            elif field['type'] == 'date':
                try:
                    if not isinstance(value, str) or not re.fullmatch(r'\d{2}/\d{2}/\d{4}', value):
                        raise ValueError()
                    datetime.strptime(value, '%d/%m/%Y')
                except ValueError:
                    errors.append(f'{key}: expected valid DD/MM/AAAA')
            elif field['type'] == 'text' and (not isinstance(value, str) or not value.strip()):
                errors.append(f'{key}: expected nonempty text')
            elif field['type'] == 'number' and (type(value) not in (int, float) or not math.isfinite(value)):
                errors.append(f'{key}: expected finite number')
            if key not in SUBJECTIVE and (not valid_ids or not ids):
                errors.append(f'{key}: documented value requires evidence')
    if relations:
        errors.extend(validate_relations(result, fields))
    return errors


def validate_relations(result, fields):
    errors = []
    by_key = {f['key']: f for f in fields}
    def value(key):
        item = result.get(key)
        return item.get('valor') if isinstance(item, dict) else None
    for field in fields:
        key, val = field['key'], value(field['key'])
        if key not in result:
            continue
        for ancestor in field['ancestors']:
            parent = ancestor['key']
            if ancestor['type'] != 'leaf' or parent not in result:
                continue
            if field['type'] == 'leaf' and (val is True or val == 'unknown') and value(parent) is not True:
                errors.append(f'{key}: active child requires parent {parent}=true')
            if value(parent) is False:
                if field['type'] == 'leaf' and val is not False:
                    errors.append(f'{key}: No parent {parent} requires No child')
                if field['type'] != 'leaf' and (val is not None or result[key].get('motivo_nulo') != 'no_aplica'):
                    errors.append(f'{key}: No parent {parent} requires null/no_aplica')
        if val is True or val == 'unknown':
            for other in field.get('mutuallyExclusiveWith', []):
                if other in by_key and (value(other) is True or value(other) == 'unknown'):
                    errors.append(f'{key}: unresolved application exclusion with {other}; review required')
    if value('egreso.estado_vital') == 'Fallecido' and 'egreso.destino' in result:
        dest = result['egreso.destino']
        if isinstance(dest, dict) and (dest.get('valor') is not None or dest.get('motivo_nulo') != 'no_aplica'):
            errors.append('egreso.destino: deceased requires null/no_aplica')
    return errors


def canonicalize(result, index):
    # Never accept model-authored quotes. Keep discontiguous evidence separate.
    return {key: {**entry, 'evidencias': [
        {'evidence_id': i, **index[i]} for i in entry['evidence_ids']
    ]} for key, entry in result.items()}


def protocol_hash():
    fields, common = bundle()
    source = (ROOT / 'protocol.py').read_text()
    return digest(json.dumps(fields, ensure_ascii=False) + common + source)


if __name__ == '__main__':
    fields, common = bundle()
    batches = groups(fields)
    for group in batches:
        (ROOT / 'prompts' / (group['name'] + '.md')).write_text(render_prompt(common, group))
    info = {'version': VERSION, 'variables': len(fields), 'groups': len(batches),
            'protocol_sha256': protocol_hash(), 'manual_sha256': digest((ROOT / 'manual-anotacion.md').read_text()),
            'source_frontend_commit': '42d550f',
            'groups_detail': [{'name': g['name'], 'keys': [f['key'] for f in g['fields']]} for g in batches]}
    (ROOT / 'manifest.json').write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k:v for k,v in info.items() if k!='groups_detail'}, indent=2))
