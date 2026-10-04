import json
import tempfile
import unittest
from pathlib import Path
from protocol import bundle, groups
import batch_driver
from batch_driver import run_arm
from sensitivity_feedback import rejection_reason, informative, run_arm_s1, ROOT_ERROR, Plan
from batch_driver import Unit
from test_protocol import empty_completed
from test_arm_c import valid_case_model, NOTE
from test_batch_driver import config_for


def duplicate_first_key(body):
    key = next(iter(body))
    return json.dumps(body, ensure_ascii=False)[:-1] + ', ' + json.dumps(key) + ': ' + json.dumps(body[key]) + '}'


class DuplicateKeyEngine:
    """Valid JSON everywhere, except units in `stubborn` (per case), which repeat a key on every attempt
    unless the retry names the duplicate key."""
    setup = {'engine': 'fake-dup'}

    def __init__(self, stubborn):
        self.fields, _ = bundle()
        self.answers = empty_completed(self.fields)
        self.stubborn, self.batches = stubborn, []

    def generate(self, requests):
        self.batches.append(len(requests))
        out = []
        for prompt, note, budget in requests:
            if 'Tarea de esta llamada: modelo del caso' in prompt:
                name, body = 'case_model', valid_case_model()
            elif '## Grupo ' in prompt:
                g = next(g for g in groups(self.fields) if '## Grupo ' + g['name'] + '\n' in prompt)
                name, body = g['name'], {f['key']: self.answers[f['key']] for f in g['fields']}
            else:
                name, body = 'form', dict(self.answers)
            case = 'SYN2' if note.rstrip().endswith('Control.') else 'SYN1'
            told = 'aparece más de una vez' in prompt
            raw = (duplicate_first_key(body) if (case, name) in self.stubborn and not told
                   else json.dumps(body, ensure_ascii=False))
            out.append((raw, {'tokens_entrada': 10, 'tokens_salida': 5, 'latencia_s': 0.1,
                              'finish_reason': 'json_complete', 'truncated': False}))
        return out


class RejectionReasonTests(unittest.TestCase):
    def test_reasons(self):
        self.assertIn('"x" aparece más de una vez', rejection_reason('{"a": {"x": 1, "x": 2}}'))
        self.assertIn('se cortó', rejection_reason('```json\n{"a": "texto sin cerrar'))
        self.assertIn('texto después', rejection_reason('{"a": 1} listo'))
        self.assertIn('raíz no es un objeto', rejection_reason('[1, 2]'))
        self.assertIn('JSON inválido', rejection_reason('{"a": 1,, "b": 2}'))
        self.assertIn('vacía', rejection_reason('  '))

    def test_informative_only_touches_rejections(self):
        check = informative(lambda out: [ROOT_ERROR + ' (strict parser)'] if isinstance(out, str) else ['k: missing field'])
        self.assertIn('motivo: la clave "a"', check('{"a": 1, "a": 2}')[0])
        self.assertEqual(check({'b': 1}), ['k: missing field'])


class ReplayDecisionTests(unittest.TestCase):
    def decide(self, modes):
        case = type('C', (), {'pid': 'X'})()
        unit = Unit(case, 'group', 'g', 'P', 10, lambda out: [ROOT_ERROR + ' (strict parser)'] if isinstance(out, str) else [])
        traces = [{'attempt': i, 'prompt': 'P', 'output': '{"a": 1, "a": 2}' if m == 'rejected' else {'a': 1},
                   'errors': ['e'], 'inference': {'parse_mode': m}} for i, m in enumerate(modes, 1)]
        plan = Plan(max_retries=2)
        plan.decide(unit, [f'p{i}' for i in range(len(modes))], traces)
        return plan, unit

    def test_rejection_only_at_last_attempt_is_reused(self):
        plan, unit = self.decide(['fenced', 'fenced', 'rejected'])
        self.assertEqual(plan.reused, ['g'])
        self.assertTrue(unit.done)

    def test_replay_starts_after_first_rejection(self):
        plan, unit = self.decide(['fenced', 'rejected', 'fenced'])
        self.assertEqual((plan.rerun, plan.replayed_from, plan.copies), (['g'], {'g': 2}, ['p0', 'p1']))
        self.assertEqual(unit.attempts, 2)
        self.assertFalse(unit.done)
        self.assertIn('motivo: la clave "a"', unit.errors[0])


class SensitivityRunTests(unittest.TestCase):
    def setUp(self):
        self.fields, self.common = bundle()
        self.rows = [{'patient_id': 'SYN1', 'text': NOTE}, {'patient_id': 'SYN2', 'text': NOTE + '\nControl.'}]

    def run_both(self, arm, stubborn):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        temp = Path(holder.name)
        primary_cfg = config_for(temp / 'primary')
        primary = DuplicateKeyEngine(stubborn)
        run_arm(arm, self.rows, primary, primary_cfg, 'fake', self.fields, self.common)
        s1 = DuplicateKeyEngine(stubborn)
        loads = []
        summary = run_arm_s1(arm, self.rows, lambda: loads.append(1) or s1, primary_cfg, 'fake', self.fields,
                             self.common, str(temp / 'primary'), str(temp / 's1'), plan_only=False)
        read = lambda root: {r['patient_id']: json.loads(
            (batch_driver.arm_directory({'output_dir': str(root)}, arm, 'fake', primary_cfg['generation'])
             / 'results' / (r['patient_id'] + '.json')).read_text()) for r in self.rows}
        return summary, read(temp / 'primary'), read(temp / 's1'), s1, loads

    def test_harness_reruns_only_the_rejected_group(self):
        summary, primary, s1, engine, loads = self.run_both('harness', {('SYN1', 'soporte_01')})
        self.assertFalse(primary['SYN1']['valid'])          # generic message: same response three times
        self.assertTrue(s1['SYN1']['valid'])                # the named cause lets the retry fix it
        self.assertEqual(s1['SYN1']['units_rerun'], ['soporte_01'])
        self.assertEqual(len(s1['SYN1']['units_reused']), 17)
        self.assertEqual(engine.batches, [1])               # attempt 1 replayed; only the retry is generated
        self.assertEqual(s1['SYN1']['replayed_from_attempt'], {'soporte_01': 1})
        self.assertEqual(s1['SYN1']['new_calls'], primary['SYN1']['new_calls'] - 1)
        self.assertEqual(summary['reused_cases_identical'], 1)
        for k in ('valid', 'errors', 'annotations', 'new_calls'):
            self.assertEqual(s1['SYN2'][k], primary['SYN2'][k])
        self.assertEqual(s1['SYN2']['pipeline'], 'form199-v2.2-s1')

    def test_armc_regenerated_case_model_regenerates_all_groups(self):
        summary, primary, s1, engine, loads = self.run_both('armc', {('SYN1', 'case_model')})
        self.assertFalse(primary['SYN1']['case_model_valid'])
        self.assertTrue(s1['SYN1']['case_model_valid'] and s1['SYN1']['valid'])
        self.assertEqual(s1['SYN1']['units_rerun'], ['case_model'])
        self.assertEqual(len(s1['SYN1']['units_new']), 18)
        self.assertEqual(engine.batches, [1, 18])
        self.assertEqual(s1['SYN2']['annotations'], primary['SYN2']['annotations'])

    def test_nothing_rejected_means_no_model_and_identical_results(self):
        summary, primary, s1, engine, loads = self.run_both('monolithic', set())
        self.assertEqual(loads, [])
        self.assertEqual(summary['reused_cases_identical'], 2)
        for pid in ('SYN1', 'SYN2'):
            self.assertEqual(s1[pid]['annotations'], primary[pid]['annotations'])


if __name__ == '__main__':
    unittest.main()
