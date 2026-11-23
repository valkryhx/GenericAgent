"""Isolated copies of plain native history with a conservative fallback."""


class NativeHistoryProjection:
    def __init__(self):
        self._prefix = []

    def normalize(self, history, *, preserve_strings=False):
        prefix = self._prefix
        start = 0
        for message, cached in zip(history, prefix):
            if message != cached[0]:
                break
            start += 1
        if start < len(prefix):
            del prefix[start:]
        for message in history[start:]:
            if type(message) is not dict or set(message) != {'role', 'content'}:
                prefix.clear()
                return None
            role, content = message['role'], message['content']
            if type(role) is not str or role not in ('user', 'assistant'):
                prefix.clear()
                return None
            if type(content) is str:
                texts = (content,)
                snapshot = message.copy()
            elif type(content) is list and all(
                type(block) is dict and set(block) == {'type', 'text'}
                and block['type'] == 'text' and type(block['text']) is str for block in content
            ):
                texts = tuple(block['text'] for block in content)
                snapshot = {'role': role, 'content': [block.copy() for block in content]}
            else:
                prefix.clear()
                return None
            prefix.append((snapshot, role, texts))
        return [
            {'role': role, 'content': snapshot['content'] if preserve_strings and type(snapshot['content']) is str
             else [{'type': 'text', 'text': text} for text in texts]}
            for snapshot, role, texts in prefix
        ]
