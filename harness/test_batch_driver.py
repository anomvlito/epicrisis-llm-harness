import json
import tempfile
import unittest
from pathlib import Path
from protocol import bundle, groups
import batch_driver
from batch_driver import Case, run_arm, RUNNERS
from test_protocol import empty_completed
from test_arm_c import valid_case_model, NOTE
from engine_vllm import split_output, find_complete_json_end, build_messages


class FakeEngine:
    """Answers every prompt with valid JSON; fails the first attempt of the units named in fail_once."""
    setup = {'engine': 'fake'}

    def __init__(self, fail_once=()):
        self.fields, _ = bundle()
        self.answers = empty_completed(self.fields)
        self.fail_once, self.seen, self.batches = set(fail_once), set(), []

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
            if name in self.fail_once and name not in self.seen:
                self.seen.add(name)
                raw = 'Aquí está: ' + json.dumps(body)            # rejected by the strict parser
            else:
                raw = '```json\n' + json.dumps(body, ensure_ascii=False)
            out.append((raw, {'tokens_entrada': 10, 'tokens_salida': 5, 'latencia_s': 0.1,
                              'finish_reason': 'json_complete', 'truncated': False}))
        return out


def config_for(temp):
    return {'output_dir': str(temp), 'engine': {'name': 'fake'},
            'generation': {'max_new_tokens': 4096, 'max_retries': 2, 'case_model_max_new_tokens': 8192,
                           'monolithic_max_new_tokens': 16384}}


class BatchDriverTests(unittest.TestCase):
    def setUp(self):
        self.fields, self.common = bundle()
        self.rows = [{'patient_id': 'SYN1', 'text': NOTE}, {'patient_id': 'SYN2', 'text': NOTE + '\nControl.'}]

    def run_arm_in(self, temp, arm, engine):
        run_arm(arm, self.rows, engine, config_for(temp), 'fake', self.fields, self.common)
        d = batch_driver.arm_directory(config_for(temp), arm, 'fake', config_for(temp)['generation'])
        return d, {r['patient_id']: json.loads((d / 'results' / (r['patient_id'] + '.json')).read_text()) for r in self.rows}

    def test_harness_batches_all_groups_of_all_cases_and_retries(self):
        engine = FakeEngine(fail_once={'soporte_01'})
        with tempfile.TemporaryDirectory() as temp:
            d, results = self.run_arm_in(Path(temp), 'harness', engine)
            traces = list((d / 'checkpoints').glob('*/traces/*.json'))
        self.assertEqual(engine.batches, [36, 1])               # 18 groups x 2 cases, then one retry
        self.assertTrue(all(r['valid'] and r['valid_fields'] == 199 for r in results.values()))
        self.assertEqual(results['SYN1']['new_calls'] + results['SYN2']['new_calls'], 37)
        self.assertEqual(len(traces), 37)

    def test_monolithic_and_armc_layouts(self):
        engine = FakeEngine(fail_once={'case_model'})
        with tempfile.TemporaryDirectory() as temp:
            dm, mono = self.run_arm_in(Path(temp), 'monolithic', engine)
            dc, armc = self.run_arm_in(Path(temp), 'armc', engine)
            self.assertTrue((dm / 'traces' / 'SYN1').exists())
            self.assertTrue((dc / 'case_models' / 'SYN1.json').exists())
        self.assertTrue(all(r['valid'] and r['strategy'] == 'monolithic' for r in mono.values()))
        self.assertTrue(all(r['valid'] and r['case_model_valid'] and r['strategy'] == 'armc' for r in armc.values()))
        self.assertEqual(sorted(r['new_calls'] for r in armc.values()), [19, 20])

    def test_boolean_null_with_reason_is_normalized_before_validation(self):
        engine = FakeEngine()
        leaf = next(f['key'] for f in self.fields if f['type'] == 'leaf')
        engine.answers[leaf] = {**engine.answers[leaf], 'valor': None, 'motivo_nulo': 'no_aplica'}
        with tempfile.TemporaryDirectory() as temp:
            d, results = self.run_arm_in(Path(temp), 'harness', engine)
            metas = [json.loads(p.read_text())['inference'] for p in (d / 'checkpoints').glob('*/traces/*.json')]
        self.assertTrue(all(r['valid'] for r in results.values()))
        self.assertEqual(engine.batches, [36])                       # no retry needed
        self.assertEqual(sum(m['boolean_labels_normalized'] for m in metas), 2)
        self.assertIs(results['SYN1']['annotations'][leaf]['valor'], False)

    def test_resume_skips_finished_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            self.run_arm_in(Path(temp), 'harness', FakeEngine())
            engine = FakeEngine()
            self.run_arm_in(Path(temp), 'harness', engine)
        self.assertEqual(engine.batches, [])

    def test_chunks_write_results_per_chunk(self):
        engine = FakeEngine()
        with tempfile.TemporaryDirectory() as temp:
            config = {**config_for(Path(temp)), 'chunk_size': 1}
            run_arm('harness', self.rows, engine, config, 'fake', self.fields, self.common)
        self.assertEqual(engine.batches, [18, 18])              # one batch per chunk of one case

    def test_failed_unit_is_missing_not_negative(self):
        class Rejecting(FakeEngine):
            def generate(self, requests):
                return [('no JSON', {}) for _ in requests]
        with tempfile.TemporaryDirectory() as temp:
            _, results = self.run_arm_in(Path(temp), 'harness', Rejecting())
        self.assertTrue(all(not r['valid'] and r['valid_fields'] == 0 and r['annotations'] == {}
                            for r in results.values()))
        self.assertEqual(results['SYN1']['new_calls'], 18 * 3)


class EngineHelpersTests(unittest.TestCase):
    def test_split_output_cuts_after_json_and_reports_truncation(self):
        raw, info = split_output('```json\n{"a": {"b": "}"}}\nextra', 'stop')
        self.assertEqual(raw, '```json\n{"a": {"b": "}"}}')
        self.assertEqual((info['finish_reason'], info['truncated'], info['characters_after_json']),
                         ('json_complete', False, 6))
        raw, info = split_output('```json\n{"a": 1', 'length')
        self.assertEqual((info['finish_reason'], info['truncated']), ('max_tokens', True))

    def test_messages_match_transformers_engine(self):
        self.assertEqual(len(build_messages('S', 'N', 'gemma')), 1)
        self.assertEqual([m['role'] for m in build_messages('S', 'N', 'qwen')], ['system', 'user'])
        self.assertIn('NOTA CLÍNICA COMPLETA:\nN', build_messages('S', 'N', 'llama')[1]['content'])


if __name__ == '__main__':
    unittest.main()
