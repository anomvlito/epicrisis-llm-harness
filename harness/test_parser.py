import json
import tempfile
import unittest
from pathlib import Path
from protocol import parse_model_output, strict_output, bundle, groups, normalize_boolean_labels
from runner import run_case
from test_protocol import empty_completed


class StrictParserTests(unittest.TestCase):
    def test_accepts_bare_and_single_fence(self):
        self.assertEqual(parse_model_output('{"a": 1}'), ({'a': 1}, 'bare'))
        self.assertEqual(parse_model_output('  \n{"a": 1}\n '), ({'a': 1}, 'bare'))
        # Generation stops at the end of the object, so the closing fence is usually absent.
        self.assertEqual(parse_model_output('```json\n{"a": 1}'), ({'a': 1}, 'fenced'))
        self.assertEqual(parse_model_output('```json\n{"a": 1}\n```'), ({'a': 1}, 'fenced'))
        self.assertEqual(parse_model_output('```\n{"a": 1}\n```\n'), ({'a': 1}, 'fenced'))

    def test_rejects_everything_else(self):
        for raw in ('Aquí está el JSON: {"a": 1}', '{"a": 1} listo', '{"a": 1}{"b": 2}',
                    '{"a": 1', '[{"a": 1}]', '{"a": 1, "a": 2}', '{"a": NaN}', '{"a": Infinity}',
                    '{"a": 1}\n```', '```json\n{"a": 1}\n``` extra', '```python\n{"a": 1}', '', None):
            self.assertEqual(parse_model_output(raw), (None, 'rejected'), raw)

    def test_strict_output_returns_raw_text_when_rejected(self):
        value, meta = strict_output({'raw_response': 'texto {"a": 1}'})
        self.assertEqual((value, meta['parse_mode']), ('texto {"a": 1}', 'rejected'))
        value, meta = strict_output({'raw_response': '```json\n{"a": 1}'})
        self.assertEqual((value, meta['parse_mode']), ({'a': 1}, 'fenced'))

    def test_rejected_output_is_retried_and_never_salvaged(self):
        fields, _ = bundle()
        answers = empty_completed(fields)
        note = 'Antecedentes: HTA.\nAlta de UPC vivo el 02/03/2022.'
        prompts = []
        def infer(prompt, indexed, budget):
            prompts.append(prompt)
            group = next(g for g in groups(fields) if '## Grupo ' + g['name'] + '\n' in prompt)
            body = json.dumps({f['key']: answers[f['key']] for f in group['fields']})
            raw = ('Respuesta: ' + body) if group['name'] == 'hospitalizacion_01' else body
            return strict_output({'raw_response': raw})
        with tempfile.TemporaryDirectory() as temp:
            summary = run_case({'patient_id': 'SYNTHETIC', 'text': note}, 'fake',
                               {'max_new_tokens': 4096, 'max_retries': 2}, Path(temp), infer, 'test')
        # The prose-wrapped group fails all three attempts; its 2 fields stay missing.
        self.assertFalse(summary['valid'])
        self.assertEqual(summary['valid_fields'], 197)
        self.assertEqual(summary['new_calls'], 20)
        retry = next(p for p in prompts if 'Corrige la respuesta' in p)
        self.assertIn('strict parser', retry)
        self.assertIn('Respuesta: ', retry)



class BracketedEvidenceIdTests(unittest.TestCase):
    def test_bracketed_ids_name_the_same_line_at_any_depth(self):
        raw = json.dumps({'a': {'valor': True, 'evidence_ids': ['[E0012]', 'E0040', ' [E0100] ']},
                          'b': {'eventos': [{'evidence_ids': ['[E0003]']}]}})
        value, meta = strict_output({'raw_response': raw})
        self.assertEqual(value['a']['evidence_ids'], ['E0012', 'E0040', 'E0100'])
        self.assertEqual(value['b']['eventos'][0]['evidence_ids'], ['E0003'])
        self.assertEqual(meta['evidence_ids_unbracketed'], 3)
        self.assertEqual(meta['raw_response'], raw)

    def test_other_shapes_are_left_for_the_validator(self):
        raw = json.dumps({'a': {'evidence_ids': ['[E12]', 'E0012]', '[E0012] texto', 12, '[[E0012]]']}})
        value, meta = strict_output({'raw_response': raw})
        self.assertEqual(value['a']['evidence_ids'], ['[E12]', 'E0012]', '[E0012] texto', 12, '[[E0012]]'])
        self.assertEqual(meta['evidence_ids_unbracketed'], 0)

    def test_rejected_output_counts_zero(self):
        value, meta = strict_output({'raw_response': 'texto'})
        self.assertEqual(meta['evidence_ids_unbracketed'], 0)


class BooleanLabelTests(unittest.TestCase):
    def entry(self, valor, motivo):
        return {'valor': valor, 'evidence_ids': [], 'incertidumbre': None, 'comentario': None, 'motivo_nulo': motivo}

    def test_boolean_null_with_reason_is_no_and_reason_on_no_is_dropped(self):
        types = {'a': 'leaf', 'b': 'leaf', 'c': 'leaf', 'd': 'leaf', 'e': 'date', 'f': 'leaf'}
        out = {'a': self.entry(None, 'no_aplica'), 'b': self.entry(None, 'no_documentado'),
               'c': self.entry(False, 'no_aplica'), 'd': self.entry(None, None),
               'e': self.entry(None, 'no_aplica'), 'f': self.entry(True, 'no_aplica')}
        self.assertEqual(normalize_boolean_labels(out, types), 3)
        self.assertEqual([(out[k]['valor'], out[k]['motivo_nulo']) for k in 'abc'], [(False, None)] * 3)
        self.assertEqual((out['d']['valor'], out['d']['motivo_nulo']), (None, None))   # unanswered stays an error
        self.assertEqual(out['e']['motivo_nulo'], 'no_aplica')                          # non-boolean untouched
        self.assertEqual((out['f']['valor'], out['f']['motivo_nulo']), (True, 'no_aplica'))  # contradiction left to validator
        self.assertEqual(normalize_boolean_labels('texto', types), 0)


class MissingnessSensitivityTests(unittest.TestCase):
    def test_relabels_only_reason_under_negative_parent_or_death(self):
        from summarize_runs import normalize_missingness
        from protocol import validate, evidence_index
        fields, _ = bundle()
        answers = empty_completed(fields)
        note = 'Antecedentes: HTA.\nAlta de UPC vivo el 02/03/2022.'
        child = 'soporte.respiratorio.vmi.fecha_inicio'
        answers[child] = {'valor': None, 'evidence_ids': [], 'incertidumbre': None,
                          'comentario': None, 'motivo_nulo': 'no_documentado'}
        index = evidence_index(note)[1]
        self.assertTrue(any('No parent' in e for e in validate(answers, fields, index)))
        fixed, changed = normalize_missingness(answers, fields)
        self.assertEqual(changed, 1)
        self.assertEqual(fixed[child]['motivo_nulo'], 'no_aplica')
        self.assertIsNone(fixed[child]['valor'])
        self.assertEqual(validate(fixed, fields, index), [])
        self.assertEqual(answers[child]['motivo_nulo'], 'no_documentado')  # input untouched


if __name__ == '__main__':
    unittest.main()
