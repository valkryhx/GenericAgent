import unittest

from tests.probes.python.stream_latency_baseline import replay


class StreamLatencyProbeTest(unittest.TestCase):
    def test_legacy_gate_is_observable_before_the_generator_finishes(self):
        result = replay(('甲', '乙' * 14, '丙', '丁'), mode='legacy')
        self.assertEqual(result['prefixes'], ['', '', '甲' + '乙' * 14 + '丙', '甲' + '乙' * 14 + '丙'])
        self.assertEqual(result['delta_count'], 2)
        self.assertEqual(result['text_chars'], 17)
        self.assertEqual(list(result['first_ms']), [
            'provider_first_chunk', 'python_display_enqueue', 'bridge_emit', 'assistant_done',
        ])

    def test_ink_frame_mode_preserves_python_batching(self):
        result = replay(('界', '', '。', '末'), mode='frame')
        self.assertEqual(result['prefixes'], ['', '', '', ''])
        self.assertEqual(result['delta_count'], 1)

    def test_frame_transport_does_not_recompute_context_for_every_chunk(self):
        from unittest.mock import patch
        from genericagent.core import context

        chunks = ('界' * 16,) * 32
        with patch('frontends.ink.bridge.time.monotonic', return_value=100), patch.object(
            context, 'context_usage', wraps=context.context_usage
        ) as measure:
            result = replay(chunks, mode='frame')
        self.assertEqual(result['delta_count'], 32)
        self.assertLess(measure.call_count, 32)
        self.assertGreaterEqual(measure.call_count, 2)  # first delta and done

    def test_probe_refuses_a_stream_with_no_display_evidence(self):
        with self.assertRaisesRegex(AssertionError, 'no assistant delta'):
            replay(('',), mode='legacy')
