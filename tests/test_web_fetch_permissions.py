import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.core.handler import GenericAgentHandler  # noqa: E402
from genericagent.core.loop import StepOutcome, exhaust  # noqa: E402
from genericagent.core.permissions.policy import READ_ONLY, build_permission_mode_policy  # noqa: E402
from genericagent.workflow.permissions import ToolPermissionPolicy  # noqa: E402


class ParentStub:
    task_dir = ""
    verbose = False

    def __init__(self):
        self.llmclient = SimpleNamespace(backend=SimpleNamespace(history=[]))


class WebFetchPermissionTest(unittest.TestCase):
    def response(self):
        return SimpleNamespace(content="")

    def test_read_only_mode_treats_web_fetch_as_read(self):
        handler = GenericAgentHandler(ParentStub())
        handler.permission_mode_policy = build_permission_mode_policy(READ_ONLY)
        with patch.object(
            GenericAgentHandler,
            "do_web_fetch",
            return_value=StepOutcome({"status": "success"}),
        ) as fetch:
            outcome = exhaust(handler.dispatch("web_fetch", {"url": "https://example.com/"}, self.response()))
        self.assertEqual({"status": "success"}, outcome.data)
        fetch.assert_called_once()

    def test_workflow_read_only_policy_allows_web_fetch(self):
        handler = GenericAgentHandler(ParentStub())
        handler.workflow_permission_policy = ToolPermissionPolicy(profile="read_only")
        with patch.object(
            GenericAgentHandler,
            "do_web_fetch",
            return_value=StepOutcome({"status": "success"}),
        ) as fetch:
            outcome = exhaust(handler.dispatch("web_fetch", {"url": "https://example.com/"}, self.response()))
        self.assertEqual({"status": "success"}, outcome.data)
        fetch.assert_called_once()

    def test_capability_denial_prevents_handler_invocation(self):
        handler = GenericAgentHandler(ParentStub())
        handler.capability_profile = SimpleNamespace(allows=lambda _name: False)
        with patch.object(GenericAgentHandler, "do_web_fetch") as fetch:
            outcome = exhaust(handler.dispatch("web_fetch", {"url": "https://example.com/"}, self.response()))
        fetch.assert_not_called()
        self.assertEqual("error", outcome.data["status"])

    def test_handler_returns_step_outcome_with_fetch_payload(self):
        handler = GenericAgentHandler(ParentStub())
        payload = {"status": "success", "result": "ok"}
        with patch("genericagent.tools.web_fetch.tool.run_web_fetch", return_value=payload):
            generator = handler.do_web_fetch({"url": "https://example.com/"}, self.response())
            chunks = []
            try:
                while True:
                    chunks.append(next(generator))
            except StopIteration as stop:
                outcome = stop.value

        self.assertEqual(payload, outcome.data)
        self.assertTrue(any("Fetching" in str(item) for item in chunks))


if __name__ == "__main__":
    unittest.main()
