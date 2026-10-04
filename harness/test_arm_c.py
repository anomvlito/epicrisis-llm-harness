import copy
import json
import tempfile
import unittest
from pathlib import Path
from protocol import bundle, groups, evidence_index
from arm_c import validate_case_model, run_case_c, CASE_MODEL_PREAMBLE
from runner import select_notes
from test_protocol import empty_completed

NOTE = ('Antecedentes: HTA.\nIngresa a UCI el 01/03/2022 desde urgencias.\n'
        'Neumonía con sepsis, conectado a VMI.\nAlta de UCI vivo el 05/03/2022 a sala.')


def valid_case_model():
    return {
        'episodio': {'hubo_estadia_uci': True, 'primera_estadia': 1,
                     'estadias_uci': [{'orden': 1, 'ingreso': '01/03/2022', 'egreso': '05/03/2022',
                                       'origen': 'urgencias', 'destino': 'sala', 'evidence_ids': ['E0002', 'E0004']}]},
        'linea_de_tiempo': [{'momento': 'durante_primera_uci', 'dia_relativo': 0, 'evento': 'inicio VMI',
                             'tipo': 'soporte', 'certeza': 'afirmado', 'evidence_ids': ['E0003']}],
        'problemas': [{'problema': 'HTA', 'estado': 'antecedente_cronico', 'certeza': 'afirmado',
                       'evidence_ids': ['E0001']}],
        'soportes': [{'soporte': 'VMI', 'realizado': True, 'inicio': None, 'fin': None, 'evidence_ids': ['E0003']}],
        'infecciones': [{'diagnostico': 'neumonía', 'sepsis': True, 'foco': 'pulmonar', 'germen': None,
                         'tratamiento': None, 'evidence_ids': ['E0003']}],
        'desenlace_primera_uci': {'estado_vital': 'Vivo', 'destino': 'sala', 'evidence_ids': ['E0004']},
        'calidad_documental': {'secciones_vacias': [], 'abreviaturas': {'VMI': 'ventilación mecánica invasiva'},
                               'contradicciones': [], 'no_documentado': ['germen']},
        'dudas': [],
    }


class CaseModelValidationTests(unittest.TestCase):
    def setUp(self):
        _, self.index = evidence_index(NOTE)

    def test_valid_case_model(self):
        self.assertEqual(validate_case_model(valid_case_model(), self.index), [])

    def test_rejects_missing_or_invalid_evidence(self):
        cm = valid_case_model(); cm['problemas'][0]['evidence_ids'] = []
        self.assertTrue(validate_case_model(cm, self.index))
        cm = valid_case_model(); cm['soportes'][0]['evidence_ids'] = ['E9999']
        self.assertTrue(validate_case_model(cm, self.index))

    def test_rejects_enum_date_and_structure_errors(self):
        for mutate in (lambda c: c['linea_de_tiempo'][0].update(momento='durante'),
                       lambda c: c['problemas'][0].update(estado='cronico'),
                       lambda c: c['soportes'][0].update(inicio='2022-03-01'),
                       lambda c: c['infecciones'][0].update(sepsis='si'),
                       lambda c: c.update(extra=1),
                       lambda c: c.pop('dudas')):
            cm = valid_case_model(); mutate(cm)
            self.assertTrue(validate_case_model(cm, self.index))

    def test_icu_consistency(self):
        cm = valid_case_model(); cm['episodio']['hubo_estadia_uci'] = False
        self.assertTrue(validate_case_model(cm, self.index))
        cm = valid_case_model()
        cm['episodio'] = {'hubo_estadia_uci': False, 'estadias_uci': [], 'primera_estadia': None}
        self.assertTrue(any('without an ICU stay' in e for e in validate_case_model(cm, self.index)))
        cm['desenlace_primera_uci'] = {'estado_vital': None, 'destino': None, 'evidence_ids': []}
        self.assertEqual(validate_case_model(cm, self.index), [])

    def test_rejects_unparsed_output(self):
        self.assertTrue(validate_case_model('texto libre', self.index))


class ArmCRunTests(unittest.TestCase):
    def setUp(self):
        self.fields, _ = bundle()
        self.answers = empty_completed(self.fields)
        self.row = {'patient_id': 'SYNTHETIC', 'text': NOTE}
        self.settings = {'max_new_tokens': 4096, 'max_retries': 2, 'prefill_chunk_size': 1024,
                         'case_model_max_new_tokens': 8192}

    def infer_factory(self, case_models):
        prompts = []
        def infer(prompt, note, budget):
            prompts.append((prompt, budget))
            if '## Grupo ' not in prompt:
                return copy.deepcopy(case_models.pop(0)), {}
            group = next(g for g in groups(self.fields) if '## Grupo ' + g['name'] + '\n' in prompt)
            return {f['key']: self.answers[f['key']] for f in group['fields']}, {}
        return infer, prompts

    def test_case_model_then_groups_with_shared_context(self):
        infer, prompts = self.infer_factory([valid_case_model()])
        with tempfile.TemporaryDirectory() as temp:
            summary = run_case_c(self.row, 'syn', self.settings, Path(temp), infer, 'id')
            result = json.loads((Path(temp) / 'results/SYNTHETIC.json').read_text())
            again = run_case_c(self.row, 'syn', self.settings, Path(temp), infer, 'id')
        self.assertTrue(summary['case_model_valid'])
        self.assertEqual(summary['new_calls'], 19)
        self.assertEqual(prompts[0][1], 8192)
        group_prompts = [p for p, _ in prompts[1:]]
        self.assertEqual(len(group_prompts), 18)
        self.assertTrue(all(CASE_MODEL_PREAMBLE in p and '"hubo_estadia_uci": true' in p for p in group_prompts))
        self.assertEqual(result['strategy'], 'armc')
        self.assertEqual(again['new_calls'], 0)

    def test_invalid_case_model_fails_case_without_group_calls(self):
        bad = valid_case_model(); bad['problemas'][0]['evidence_ids'] = []
        infer, prompts = self.infer_factory([bad, bad, bad])
        with tempfile.TemporaryDirectory() as temp:
            summary = run_case_c(self.row, 'syn', self.settings, Path(temp), infer, 'id')
            result = json.loads((Path(temp) / 'results/SYNTHETIC.json').read_text())
        self.assertFalse(summary['valid'])
        self.assertFalse(result['case_model_valid'])
        self.assertEqual(len(prompts), 3)
        self.assertEqual(result['annotations'], {})

    def test_retry_recovers_case_model(self):
        bad = valid_case_model(); bad['soportes'][0]['evidence_ids'] = ['E9999']
        infer, prompts = self.infer_factory([bad, valid_case_model()])
        with tempfile.TemporaryDirectory() as temp:
            summary = run_case_c(self.row, 'syn', self.settings, Path(temp), infer, 'id')
        self.assertTrue(summary['case_model_valid'])
        self.assertIn('Corrige el modelo del caso', prompts[1][0])

    def test_select_notes_by_case_id(self):
        notes = [{'patient_id': 'A'}, {'patient_id': 'B'}]
        self.assertEqual(select_notes(notes, 0, 'B'), [{'patient_id': 'B'}])
        with self.assertRaises(ValueError):
            select_notes(notes, 0, 'C')


if __name__ == '__main__':
    unittest.main()
