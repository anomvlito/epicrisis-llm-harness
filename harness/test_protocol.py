import copy
import json
import tempfile
import unittest
from pathlib import Path
from protocol import (bundle, groups, evidence_index, validate, canonicalize,
                      render_prompt, protocol_hash, ROOT)
from runner import run_case


def response(value=False, ids=None, reason=None, uncertainty=None):
    return {'valor': value, 'evidence_ids': ids or [], 'incertidumbre': uncertainty,
            'comentario': None, 'motivo_nulo': reason}


def empty_completed(fields):
    result = {}
    for f in fields:
        if f['type'] == 'leaf':
            result[f['key']] = response(False)
        elif f['key'] == 'calidad.global':
            result[f['key']] = response('parcial')
        else:
            reason = ('no_aplica' if any(a['type']=='leaf' for a in f['ancestors'])
                      else 'no_documentado')
            if f['key'] == 'calidad.comentario':
                reason = 'opcional'
            result[f['key']] = response(None, reason=reason)
    return result


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.fields, self.common = bundle()
        self.field_map = {f['key']:f for f in self.fields}
        self.note = 'Antecedentes: HTA.\n\nInfección respiratoria en estudio.\nAlta de UPC vivo el 02/03/2022.'
        self.indexed, self.index = evidence_index(self.note)
        self.answers = empty_completed(self.fields)
        self.hta = 'antecedentes.cardiovascular.hipertension_arterial'

    def test_exact_app_keys_and_types_without_legacy_fields(self):
        self.assertEqual(len(self.fields),199)
        self.assertEqual(sum(f['type']=='leaf' for f in self.fields),174)
        self.assertNotIn('insuficiencia_cardiaca_clase_iv',self.field_map)
        self.assertIn('antecedentes.cardiovascular.insuficiencia_cardiaca',self.field_map)
        flattened=[f['key'] for g in groups(self.fields) for f in g['fields']]
        self.assertEqual(flattened,[f['key'] for f in self.fields])
        for g in groups(self.fields):
            self.assertLessEqual(len(g['fields']),16)
            self.assertEqual((ROOT/'prompts'/(g['name']+'.md')).read_text(), render_prompt(self.common,g))

    def test_no_evidence_required_for_no_and_no_null_boolean(self):
        self.assertEqual(validate(self.answers,self.fields,self.index),[])
        self.answers[self.hta]=response(None)
        self.assertTrue(any('null is unanswered' in e for e in validate(self.answers,self.fields,self.index)))

    def test_yes_requires_evidence_and_bool_not_integer(self):
        self.answers[self.hta]=response(True)
        self.assertTrue(validate(self.answers,self.fields,self.index))
        self.answers[self.hta]=response(True,['E0001'])
        self.assertEqual(validate(self.answers,self.fields,self.index),[])
        self.answers[self.hta]['valor']=1
        self.assertTrue(validate(self.answers,self.fields,self.index))

    def test_doubt_requires_evidence_and_uncertainty(self):
        self.answers[self.hta]=response('unknown',['E0001'],uncertainty='Indeterminado')
        self.assertEqual(validate(self.answers,self.fields,self.index),[])
        self.answers[self.hta]['incertidumbre']=None
        self.assertTrue(validate(self.answers,self.fields,self.index))
        self.answers[self.hta]=response('unknown',uncertainty='Alto')
        self.assertTrue(validate(self.answers,self.fields,self.index))

    def test_discontiguous_evidence_keeps_original_offsets(self):
        self.answers[self.hta]=response(True,['E0001','E0004'])
        converted=canonicalize(self.answers,self.index)[self.hta]['evidencias']
        self.assertEqual(len(converted),2)
        for span in converted:
            self.assertEqual(self.note[span['start']:span['end']],span['text'])
        self.assertNotIn('E0002',self.index)

    def test_invalid_evidence_types_and_ids_rejected(self):
        for ids in (['E9999'],[{}],'E0001',['E0001','E0001']):
            self.answers[self.hta]=response(True)
            self.answers[self.hta]['evidence_ids']=ids
            self.assertTrue(validate(self.answers,self.fields,self.index))

    def test_extra_or_missing_fields_and_model_quotes_rejected(self):
        del self.answers[self.hta]
        self.assertTrue(validate(self.answers,self.fields,self.index))
        self.answers=empty_completed(self.fields)
        self.answers[self.hta]['evidencia']='inventada'
        self.assertTrue(validate(self.answers,self.fields,self.index))
        self.answers=empty_completed(self.fields)
        self.answers['legacy']=response()
        self.assertTrue(validate(self.answers,self.fields,self.index))

    def test_real_dates_and_exact_choices(self):
        key='egreso.fecha_egreso_upc'
        self.answers[key]=response('31/02/2022',['E0004'])
        self.assertTrue(validate(self.answers,self.fields,self.index))
        self.answers[key]=response('02/03/2022',['E0004'])
        self.answers['egreso.estado_vital']=response('Vivo',['E0004'])
        self.assertEqual(validate(self.answers,self.fields,self.index),[])
        self.answers['egreso.estado_vital']['valor']='vivo'
        self.assertTrue(validate(self.answers,self.fields,self.index))

    def test_missingness_types_and_quality_judgment(self):
        key='ingreso.fecha_ingreso_upc'
        self.answers[key]['motivo_nulo']={}
        self.assertTrue(validate(self.answers,self.fields,self.index))
        self.answers[key]=response(None,reason='no_documentado')
        self.answers['calidad.global']=response(None,reason='no_documentado')
        self.assertTrue(validate(self.answers,self.fields,self.index))

    def test_nonboolean_documented_value_requires_evidence(self):
        self.answers['ingreso.diagnostico.principal']=response('Diagnóstico')
        self.assertTrue(validate(self.answers,self.fields,self.index))

    def test_positive_child_cannot_have_negative_parent(self):
        child='soporte.respiratorio.vmi'
        self.answers[child]=response(True,['E0001'])
        self.assertTrue(any('parent' in e for e in validate(self.answers,self.fields,self.index)))
        self.answers['soporte.respiratorio']=response(True,['E0001'])
        for f in self.fields:
            if any(a['key']==child for a in f['ancestors']) and f['type']!='leaf':
                self.answers[f['key']]=response(None,reason='no_documentado')
        self.assertEqual(validate(self.answers,self.fields,self.index),[])

    def test_exclusive_clinical_findings_flagged_not_deleted(self):
        for key in ('soporte.respiratorio','soporte.respiratorio.vmni','soporte.respiratorio.vmi'):
            self.answers[key]=response(True,['E0001'])
        before=copy.deepcopy(self.answers)
        self.assertTrue(any('exclusion' in e for e in validate(self.answers,self.fields,self.index)))
        self.assertEqual(before,self.answers)

    def test_deceased_destination_not_applicable(self):
        self.answers['egreso.estado_vital']=response('Fallecido',['E0004'])
        self.assertTrue(validate(self.answers,self.fields,self.index))
        self.answers['egreso.destino']=response(None,reason='no_aplica')
        self.assertEqual(validate(self.answers,self.fields,self.index),[])

    def test_runner_full_case_and_resume_without_inference(self):
        row={'patient_id':'SYNTHETIC','text':self.note}
        settings={'max_new_tokens':4096,'max_retries':1}
        calls=[]
        def infer(prompt,note,budget):
            group=next(g for g in groups(self.fields) if '## Grupo '+g['name']+'\n' in prompt)
            calls.append(group['name'])
            return {f['key']:self.answers[f['key']] for f in group['fields']},{'synthetic':True}
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            summary=run_case(row,'synthetic',settings,directory,infer,protocol_hash())
            self.assertTrue(summary['valid'])
            self.assertEqual(summary['valid_fields'],199)
            self.assertEqual(summary['new_calls'],18)
            rerun=run_case(row,'synthetic',settings,directory,infer,protocol_hash())
            self.assertEqual(rerun['new_calls'],0)
            with self.assertRaises(ValueError):
                run_case({**row,'text':self.note+' changed'},'synthetic',settings,directory,infer,protocol_hash())

    def test_incomplete_model_response_never_becomes_negative(self):
        with tempfile.TemporaryDirectory() as temp:
            result=run_case({'patient_id':'SYNTHETIC','text':self.note},'fake',
                            {'max_new_tokens':1,'max_retries':0},Path(temp),lambda *a:({},{}),'test')
            self.assertFalse(result['valid'])
            self.assertEqual(result['valid_fields'],0)
            saved=json.loads((Path(temp)/'results/SYNTHETIC.json').read_text())
            self.assertEqual(saved['annotations'],{})


if __name__=='__main__':
    unittest.main()
