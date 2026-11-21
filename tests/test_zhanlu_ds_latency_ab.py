import unittest
from unittest.mock import Mock

from tests.probes.python.zhanlu_ds_latency_ab import comparison_order, resolve_profile, summarize


class LatencyABTest(unittest.TestCase):
    def test_mismatched_profile_is_rejected_before_loading_config(self):
        load = Mock(side_effect=AssertionError('configuration must not be loaded'))
        with self.assertRaisesRegex(ValueError, 'approved profile'):
            resolve_profile('unused.yaml', 'default', load=load)
        load.assert_not_called()

    def test_same_model_on_another_provider_is_rejected(self):
        resolved = Mock(provider_key='other', wire_api='openai_chat')
        resolved.to_legacy_cfg.return_value = {
            'apibase': 'https://example.invalid', 'apikey': 'fixture', 'model': 'moma/deepseek-v4.1-flash',
        }
        config = Mock()
        config.resolve.return_value = resolved
        with self.assertRaisesRegex(ValueError, 'approved profile'):
            resolve_profile('unused.yaml', 'deep', load=lambda path: config)

    def test_rounds_counterbalance_which_mode_runs_first(self):
        self.assertEqual(list(comparison_order(3)), [
            (1, 'legacy'), (1, 'candidate'), (2, 'candidate'), (2, 'legacy'),
            (3, 'legacy'), (3, 'candidate'),
        ])

    def test_summary_does_not_copy_arbitrary_fields_from_sample_rows(self):
        rows = [{'mode': 'legacy', 'ok': True, 'ms': {'request_to_text': n}, 'hidden': 'secret'} for n in (1, 2, 100)]
        self.assertEqual(summarize(rows), {'legacy': {'runs': 3, 'passed': 3, 'ms': {
            'request_to_text': {'p50': 2, 'p90': 100},
        }}})
