import json
import unittest
from types import SimpleNamespace
from runner import prepare_model, CacheLayout
from protocol import ROOT

try:
    from transformers.cache_utils import DynamicCache
except ImportError:
    DynamicCache = None


def fake_model(**text_config):
    text = SimpleNamespace(**text_config)
    calls = []
    model = SimpleNamespace(generation_config=SimpleNamespace(prefill_chunk_size=None),
                            config=SimpleNamespace(get_text_config=lambda decoder=True: text))
    model.generate = lambda *a, **k: calls.append(k) or 'generated'
    return model, calls


GEMMA_LIKE = dict(num_kv_shared_layers=0, sliding_window=1024, num_hidden_layers=6,
                  layer_types=['sliding_attention'] * 5 + ['full_attention'])


class InferenceSetupTests(unittest.TestCase):
    def test_chunked_prefill_comes_from_versioned_config(self):
        config = json.loads((ROOT / 'config.json').read_text())
        self.assertEqual(config['generation']['prefill_chunk_size'], 1024)
        model, _ = fake_model(num_hidden_layers=2)
        setup = prepare_model(model, config['generation'])
        self.assertEqual(model.generation_config.prefill_chunk_size, 1024)
        self.assertEqual(setup, {'prefill_chunk_size': 1024, 'cache_layout_fix': False})

    def test_cache_fix_only_when_shared_layers_is_zero(self):
        for extra, expected in (({}, False), ({'num_kv_shared_layers': 2}, False), (GEMMA_LIKE, True)):
            model, calls = fake_model(**{"num_hidden_layers": 6, **extra})
            setup = prepare_model(model, {'prefill_chunk_size': 1024},
                                  cache_factory=lambda config: ('cache', config))
            model.generate(max_new_tokens=5)
            self.assertEqual(setup['cache_layout_fix'], expected)
            self.assertEqual('past_key_values' in calls[0], expected)

    def test_explicit_cache_is_not_replaced(self):
        model, calls = fake_model(**GEMMA_LIKE)
        prepare_model(model, {'prefill_chunk_size': 1024}, cache_factory=lambda config: 'new')
        model.generate(past_key_values='given')
        self.assertEqual(calls[0]['past_key_values'], 'given')

    def test_layout_hides_shared_layer_count(self):
        layout = CacheLayout(SimpleNamespace(**GEMMA_LIKE))
        self.assertFalse(hasattr(layout, 'num_kv_shared_layers'))
        self.assertIs(layout.get_text_config(decoder=True), layout)
        self.assertEqual(len(layout.layer_types), 6)

    @unittest.skipIf(DynamicCache is None, 'transformers not installed')
    def test_real_cache_keeps_sliding_layers(self):
        cache = DynamicCache(config=CacheLayout(SimpleNamespace(**GEMMA_LIKE)))
        kinds = [type(layer).__name__ for layer in cache.layers]
        self.assertEqual(kinds, ['DynamicSlidingWindowLayer'] * 5 + ['DynamicLayer'])


if __name__ == '__main__':
    unittest.main()
