import json
import tempfile
import unittest
from pathlib import Path
from protocol import bundle, groups, render_prompt, evidence_index
from monolithic import render_monolithic_prompt, run_case, GROUP_SENTENCE, FORM_SENTENCE
from test_protocol import empty_completed, response


class MonolithicTests(unittest.TestCase):
    def setUp(self):
        self.fields, self.common = bundle()
        self.note = 'Antecedentes: HTA.\n\nInfección respiratoria en estudio.\nAlta de UPC vivo el 02/03/2022.'
        self.answers = empty_completed(self.fields)
        self.row = {'patient_id': 'SYNTHETIC', 'text': self.note}
        self.settings = {'max_new_tokens': 16384, 'max_retries': 2}

    def test_prompt_has_same_catalogue_as_groups_and_no_group_header(self):
        prompt = render_monolithic_prompt(self.common, self.fields)
        self.assertNotIn('## Grupo', prompt)
        self.assertNotIn(GROUP_SENTENCE, prompt)
        self.assertIn(FORM_SENTENCE, prompt)
        catalogue = json.loads(prompt.split('son:\n', 1)[1].rsplit('\n\nLee toda', 1)[0])
        from_groups = [f for g in groups(self.fields) for f in g['fields']]
        self.assertEqual(catalogue, from_groups)
        # Every group prompt shares the instruction text that the single call adapts.
        for g in groups(self.fields):
            self.assertTrue(render_prompt(self.common, g).startswith(self.common))

    def test_valid_form_in_one_call_and_resume(self):
        calls = []
        def infer(prompt, note, budget):
            calls.append(budget)
            return dict(self.answers), {'synthetic': True}
        with tempfile.TemporaryDirectory() as temp:
            summary = run_case(self.row, 'synthetic', self.settings, Path(temp), infer, 'test')
            self.assertEqual((summary['valid'], summary['valid_fields'], summary['new_calls']), (True, 199, 1))
            self.assertEqual(calls, [16384])
            again = run_case(self.row, 'synthetic', self.settings, Path(temp), infer, 'test')
            self.assertEqual(again['new_calls'], 0)
            with self.assertRaises(ValueError):
                run_case(self.row, 'synthetic', {**self.settings, 'max_new_tokens': 4096},
                         Path(temp), infer, 'test')

    def test_retry_receives_errors_and_can_recover(self):
        prompts = []
        def infer(prompt, note, budget):
            prompts.append(prompt)
            if len(prompts) == 1:
                partial = dict(self.answers)
                partial.pop(self.fields[0]['key'])
                return partial, {}
            return dict(self.answers), {}
        with tempfile.TemporaryDirectory() as temp:
            summary = run_case(self.row, 'synthetic', self.settings, Path(temp), infer, 'test')
            self.assertTrue(summary['valid'])
            self.assertEqual(summary['new_calls'], 2)
            self.assertIn('missing field', prompts[1])
            self.assertEqual(len(list((Path(temp) / 'traces' / 'SYNTHETIC').iterdir())), 2)

    def test_truncated_form_keeps_valid_fields_and_never_imputes_no(self):
        half = {f['key']: self.answers[f['key']] for f in self.fields[:100]}
        half[self.fields[1]['key']] = response(None)  # invalid boolean
        with tempfile.TemporaryDirectory() as temp:
            summary = run_case(self.row, 'fake', {'max_new_tokens': 1, 'max_retries': 0}, Path(temp),
                               lambda *a: (half, {'truncated': True}), 'test')
            saved = json.loads((Path(temp) / 'results/SYNTHETIC.json').read_text())
        self.assertFalse(summary['valid'])
        self.assertEqual(summary['valid_fields'], 99)
        self.assertNotIn(self.fields[1]['key'], saved['annotations'])
        self.assertNotIn(self.fields[150]['key'], saved['annotations'])

    def test_unparsed_response_is_invalid_and_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            summary = run_case(self.row, 'fake', {'max_new_tokens': 1, 'max_retries': 0}, Path(temp),
                               lambda *a: ({'_raw_response': 'not json'}, {}), 'test')
        self.assertFalse(summary['valid'])
        self.assertEqual(summary['valid_fields'], 0)


if __name__ == '__main__':
    unittest.main()
