"""Static guard: ``Agent.run`` must bind ``full_resp`` before it consumes the stream.

The `except` branch of `Agent.run` builds `failed_resp = full_resp + ...`. `full_resp` is
bound only by `self._drain_agent_stream(...)`, so any exception raised *before* it returns
left the error handler with an unbound local:

    UnboundLocalError: cannot access local variable 'full_resp' where it is not associated
    with a value

That is not a cosmetic traceback. In the 15-task run (1794216552052) a `send_message` to
`/root` raised inside the tool, the stream propagated, the handler died with the
UnboundLocalError, the worker thread was gone, and the lead had to resume the teammate with
work still on the board -- while the real cause was nowhere in the log.

A behavioural test for this needs a whole `Agent.run` (model client, handler, transcript);
the invariant that actually prevents the regression is an ordering one, so it is checked on
the AST rather than by substring: comments legitimately mention `full_resp`, text matching
would pass for the wrong reason.
"""
import ast
import pathlib
import unittest


APP_PATH = pathlib.Path(__file__).resolve().parents[1] / "genericagent" / "core" / "app.py"


def _find_run(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GenericAgent":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "run":
                    return item
    return None


def _statement_blocks(tree):
    """Yield every statement list in the module (function bodies, with/for/try bodies)."""
    for node in ast.walk(tree):
        for _field, value in ast.iter_fields(node):
            if isinstance(value, list) and value and all(isinstance(item, ast.stmt) for item in value):
                yield value


def _assigns_name(statement, name):
    if not isinstance(statement, ast.Assign):
        return False
    return any(
        isinstance(target, ast.Name) and target.id == name and isinstance(target.ctx, ast.Store)
        for target in statement.targets
    )


def _mentions(node, name):
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and item.id == name:
            return True
        if isinstance(item, ast.Attribute) and item.attr == name:
            return True
    return False


class RunFullRespBindingTest(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
        self.run_fn = _find_run(self.tree)
        self.assertIsNotNone(self.run_fn, "GenericAgent.run not found")

    def _stream_try(self):
        for node in ast.walk(self.run_fn):
            if isinstance(node, ast.Try) and _mentions(node, "_drain_agent_stream"):
                return node
        self.fail("the try that consumes _drain_agent_stream is gone; revisit this guard")

    def _handler_uses_full_resp(self, try_node):
        return any(_mentions(handler, "full_resp") for handler in try_node.handlers)

    def test_the_error_handler_still_reads_full_resp(self):
        # Premise check: if the handler stops using `full_resp`, this guard no longer
        # protects anything and should be rewritten instead of silently passing.
        self.assertTrue(self._handler_uses_full_resp(self._stream_try()))

    def test_full_resp_is_bound_before_the_stream_try(self):
        stream_try = self._stream_try()
        for block in _statement_blocks(self.run_fn):
            if stream_try not in block:
                continue
            earlier = []
            for statement in block:
                if statement is stream_try:
                    break
                earlier.append(statement)
            self.assertTrue(
                any(_assigns_name(statement, "full_resp") for statement in earlier),
                "full_resp must be initialized before the try that calls _drain_agent_stream; "
                "otherwise the except branch raises UnboundLocalError and hides the real error",
            )
            return
        self.fail("could not locate the block that owns the stream try")


if __name__ == "__main__":
    unittest.main()
