import unittest

from genericagent.llm.history_projection import NativeHistoryProjection


def row(role, text):
    return {'role': role, 'content': [{'type': 'text', 'text': text}]}


class HistoryProjectionTest(unittest.TestCase):
    def test_append_reuses_immutable_prefix_and_never_exposes_cached_mutable_rows(self):
        projection = NativeHistoryProjection()
        history = [row('user', 'one'), row('assistant', 'two')]
        first = projection.normalize(history)
        self.assertEqual(first, history)
        first[0]['content'][0]['text'] = 'backend mutation'
        history.append(row('user', 'three'))
        self.assertEqual(projection.normalize(history), [row('user', 'one'), row('assistant', 'two'), row('user', 'three')])
        history[0]['content'][0]['text'] = 'same object, changed prefix'
        self.assertEqual(projection.normalize(history), history)
        self.assertEqual(projection.normalize(history[1:]), history[1:])

    def test_unsupported_blocks_force_full_conversion(self):
        projection = NativeHistoryProjection()
        for block in ({'type': 'image'}, {'type': 'tool_result'}, {'type': 'thinking'},
                      {'type': 'text', 'text': 'a', 'cache_control': {'type': 'ephemeral'}}):
            self.assertIsNone(projection.normalize([{'role': 'user', 'content': [block]}]))

    def test_legacy_string_rows_normalize_without_character_splitting(self):
        self.assertEqual(NativeHistoryProjection().normalize([{'role': 'user', 'content': '界'}]), [row('user', '界')])

    def test_native_request_uses_the_fast_copy_only_for_supported_history(self):
        from unittest.mock import patch
        from genericagent.llm import core

        class CaptureSession(core.NativeClaudeSession):
            def raw_ask(self, messages):
                self.sent = messages
                yield 'response'
                return [{'type': 'text', 'text': 'response'}]

        message = row('user', 'new prompt')
        backend = CaptureSession({'apikey': 'offline-fixture', 'apibase': 'https://example.invalid'})
        backend.history = [row('user', 'prior prompt'), row('assistant', 'prior reply')]
        with patch.dict('os.environ', {'GA_HISTORY_PROJECTION': 'incremental'}), patch.object(
            core.copy, 'deepcopy', wraps=core.copy.deepcopy
        ) as deepcopy:
            self.assertEqual(list(backend.ask(message)), ['response'])
            self.assertEqual(deepcopy.call_count, 0)
            self.assertEqual(backend.sent, [row('user', 'prior prompt'), row('assistant', 'prior reply'), message])
            backend.sent[0]['content'][0]['text'] = 'wire mutation'
            self.assertEqual(backend.history[0], row('user', 'prior prompt'))
            backend.history.append({'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'test'}]})
            self.assertEqual(list(backend.ask(message)), ['response'])
            self.assertGreater(deepcopy.call_count, 0)
