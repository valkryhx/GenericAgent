"""The fold point: a background result must surface mid-turn, not at the next user message.

GA used to consume subagent notifications only when a *user* turn started
(``build_system_prompt_sections``). A child that finished while the main agent was
still working therefore stayed invisible until the user typed something, however
many tool rounds ran in between. CC drains at every tool-round fold point, so
these tests pin the same behaviour: the notice must appear in the messages the
*next* ``chat()`` reads, and only an ack after that delivery may advance the
cursor.
"""

import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.agents.event_bus import SubagentEventBus  # noqa: E402
from genericagent.agents.notifications import peek_subagent_notifications_prompt  # noqa: E402
from genericagent.core.loop import StepOutcome, agent_runner_loop  # noqa: E402


class _ToolCall:
    def __init__(self, name, arguments="{}"):
        self.function = types.SimpleNamespace(name=name, arguments=arguments)
        self.id = "call_" + name


class _Response:
    def __init__(self, tool_calls, content=""):
        self.tool_calls = tool_calls
        self.content = content


class _RecordingClient:
    """Records every messages list it is handed, then answers from a script."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.seen = []
        self.last_tools = ""

    def chat(self, messages=None, tools=None):
        # Deep-copied so a later mutation of the loop's list cannot rewrite history.
        self.seen.append([dict(m) for m in (messages or [])])
        if False:
            yield ""
        return self.responses.pop(0) if self.responses else _Response([])


class _Handler:
    max_turns = 10

    def __init__(self, project_root):
        self.parent = types.SimpleNamespace(task_dir=None, project_root=project_root)
        self._done_hooks = []
        self.current_turn = 0
        self.lock = __import__("threading").Lock()

    def dispatch(self, tool_name, args, response, index=0, tool_num=1):
        yield tool_name + " output\n"
        return StepOutcome({"tool": tool_name}, next_prompt="\n")

    def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason):
        return next_prompt


class _ChildHandler(_Handler):
    """A subagent: it has a task_dir, so it must never consume the stream."""

    def __init__(self, project_root):
        super().__init__(project_root)
        self.parent = types.SimpleNamespace(task_dir=str(Path(project_root) / "temp" / "child"), project_root=project_root)


def _emit(root, summary, agent="worker"):
    SubagentEventBus(Path(root) / "temp" / "subagents").append_event(
        "turn_completed",
        agent_path=f"/root/{agent}",
        task_name=agent,
        payload={"summary": summary},
        notify=True,
    )


def _run(client, handler, max_turns=3):
    return list(agent_runner_loop(client, "sys", "go", handler, [], max_turns=max_turns, verbose=False))


class FoldPointInjectionTest(unittest.TestCase):
    def test_a_notification_queued_mid_turn_reaches_the_next_chat(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            # The child finishes while the main agent is on its first tool round.
            client = _RecordingClient(
                [
                    _Response([_ToolCall("slow_tool")]),  # turn 1: a tool runs, notice queued after
                    _Response([]),                       # turn 2
                ]
            )
            handler = _Handler(td)
            original_dispatch = handler.dispatch
            emitted = {"done": False}

            def dispatch(tool_name, args, response, index=0, tool_num=1):
                if not emitted["done"]:
                    _emit(td, "child finished mid-task")
                    emitted["done"] = True
                return original_dispatch(tool_name, args, response, index, tool_num)

            handler.dispatch = dispatch

            _run(client, handler, max_turns=2)

            self.assertGreaterEqual(len(client.seen), 2)
            second = client.seen[1]
            user_messages = [m for m in second if m.get("role") == "user"]
            self.assertTrue(user_messages)
            injected = user_messages[-1]["content"]
            self.assertIn("<task-notification", injected)
            self.assertIn("child finished mid-task", injected)
            # First chat could not have seen it: it did not exist yet.
            first_user = [m for m in client.seen[0] if m.get("role") == "user"][-1]["content"]
            self.assertNotIn("<task-notification", first_user)

    def test_the_notice_is_not_repeated_once_acked(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            _emit(td, "only once")
            client = _RecordingClient([_Response([]), _Response([]), _Response([])])
            handler = _Handler(td)

            _run(client, handler, max_turns=3)

            self.assertGreaterEqual(len(client.seen), 3)
            def user_text(batch):
                return [m for m in batch if m.get("role") == "user"][-1]["content"]

            # The first fold point injects it into the second chat...
            self.assertIn("<task-notification", user_text(client.seen[1]))
            # ...and because that chat succeeded, the cursor advanced, so the third
            # chat does not see it again.
            self.assertNotIn("<task-notification", user_text(client.seen[2]))
            self.assertEqual(peek_subagent_notifications_prompt(td), ("", None))

    def test_a_failed_chat_does_not_lose_the_notice(self):
        """Ack happens after chat() succeeds, so a raise replays instead of dropping."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            _emit(td, "must survive")

            class _ExplodingClient(_RecordingClient):
                def chat(self, messages=None, tools=None):
                    self.seen.append([dict(m) for m in (messages or [])])
                    if len(self.seen) == 2:
                        raise RuntimeError("provider blew up")
                    if False:
                        yield ""
                    return _Response([])

            client = _ExplodingClient([_Response([])])
            handler = _Handler(td)
            with self.assertRaises(RuntimeError):
                _run(client, handler, max_turns=3)

            # The turn died, but nothing was acked, so the notice is still there.
            text, ack_upto = peek_subagent_notifications_prompt(td)
            self.assertIn("must survive", text)
            self.assertTrue(ack_upto)

    def test_a_subagent_never_consumes_the_parents_stream(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            _emit(td, "parent only")
            client = _RecordingClient([_Response([_ToolCall("work")]), _Response([])])
            handler = _ChildHandler(td)

            _run(client, handler, max_turns=2)

            for batch in client.seen:
                for message in batch:
                    if message.get("role") == "user":
                        self.assertNotIn("<task-notification", message.get("content") or "")
            # And it did not ack on the parent's behalf either.
            self.assertTrue(peek_subagent_notifications_prompt(td)[0])

    def test_no_project_root_keeps_the_old_behaviour(self):
        """A handler without project_root must not read the real repo's stream."""
        client = _RecordingClient([_Response([_ToolCall("work")]), _Response([])])
        handler = _Handler(None)
        handler.parent = types.SimpleNamespace(task_dir=None)

        _run(client, handler, max_turns=2)

        for batch in client.seen:
            for message in batch:
                if message.get("role") == "user":
                    self.assertNotIn("<task-notification", message.get("content") or "")


if __name__ == "__main__":
    unittest.main()
