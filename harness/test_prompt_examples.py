import json
import re
import unittest
from protocol import ROOT, RESPONSE_KEYS, bundle, evidence_index, validate

NOTE = '\n'.join(f'Línea sintética {i}.' for i in range(1, 121))


class PromptExampleTests(unittest.TestCase):
    """Every example shown to the models must itself pass the validator."""

    def setUp(self):
        self.fields, self.common = bundle()
        self.by_key = {f['key']: f for f in self.fields}
        self.index = evidence_index(NOTE)[1]

    def examples(self):
        return [json.loads(m) for m in re.findall(r'\{"valor".*?"motivo_nulo": [^}]*\}', self.common)]

    def check(self, key, entry):
        return validate({key: entry}, [self.by_key[key]], self.index, relations=False)

    def test_type_examples_are_valid_for_their_type(self):
        examples = self.examples()
        self.assertGreaterEqual(len(examples), 11)
        by_type = {'leaf': 'soporte.respiratorio.vmi', 'date': 'soporte.respiratorio.vmi.fecha_inicio',
                   'select': 'egreso.estado_vital', 'text': 'soporte.respiratorio.vmi.motivo'}
        for entry in examples:
            self.assertEqual(set(entry), RESPONSE_KEYS)
            value = entry['valor']
            if value in (True, False, 'unknown'):
                key = by_type['leaf']
            elif value is None:
                key = by_type['date']
            elif re.fullmatch(r'\d\d/\d\d/\d{4}', value):
                key = by_type['date']
            elif value in self.by_key['egreso.estado_vital']['choices']:
                key = by_type['select']
            else:
                key = by_type['text']
            self.assertEqual(self.check(key, entry), [], (key, entry))

    def test_hierarchy_example_passes_with_relations(self):
        block = self.common.split('### Ejemplo de jerarquía')[1].split('Los hijos booleanos')[0]
        items = dict(re.findall(r'- (Padre booleano|Hijo booleano|Hijo de fecha|Hijo de texto) \([^)]*\): (\{.*\})', block))
        keys = {'Padre booleano': 'soporte.respiratorio.vmi', 'Hijo booleano': 'soporte.respiratorio.vmi.mas_de_un_ciclo',
                'Hijo de fecha': 'soporte.respiratorio.vmi.fecha_inicio', 'Hijo de texto': 'soporte.respiratorio.vmi.motivo'}
        self.assertEqual(set(items), set(keys))
        example = {keys[label]: json.loads(entry) for label, entry in items.items()}
        self.assertEqual(validate(example, [self.by_key[k] for k in example], self.index), [])

    def test_no_real_keys_in_examples_and_ids_are_quoted(self):
        # Real keys in examples leaked into other groups' answers in v1.7.
        section = self.common.split('## Evidencia y formato')[1]
        self.assertFalse([k for k in self.by_key if '"' + k + '"' in section or '`' + k + '`' in section])
        self.assertNotRegex(self.common, r'"evidence_ids": \[E\d')


if __name__ == '__main__':
    unittest.main()
