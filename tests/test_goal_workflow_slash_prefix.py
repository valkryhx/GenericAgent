"""Regression: a model-facing prefix must not swallow a leading slash command.

Real failure (2026-11-03): the Ink bridge prefixed "/goal ... 使用 workflow" with a
"[WORKFLOW OPT-IN]" reminder, so ``GenericAgent.run`` saw a string that did not
start with "/". ``_handle_slash_cmd`` returned it untouched, the goal was never
armed, and no goal card / met-badge ever appeared even though the workflow ran.

A second, independent failure: the Ink composer can deliver "/goal ..." with
leading whitespace, so the re-attached text is "...\n\n /goal ...". The old
``raw_query.startswith('/')`` check (no strip) missed it too.

These are deterministic (no LLM): they pin the invariant that slash commands are
parsed on the (stripped) query and the model prefix is only re-attached to the
resolved prompt/kickoff.
"""

import queue
import unittest

from genericagent.core.app import GenericAgent


def _bare_agent():
    a = GenericAgent.__new__(GenericAgent)
    a.active_goal = None
    a._queued_goal_origin = None
    a.history = []
    a.hook_runtime = None
    a.session_path = None
    a.session_id = "probe"
    a.data_root = "temp"
    return a


PREFIX = "[WORKFLOW OPT-IN]\nplease author a workflow script\n[/WORKFLOW OPT-IN]"


class SlashPrefixTests(unittest.TestCase):
    def test_prefixed_goal_still_arms_goal(self):
        a = _bare_agent()
        out = a._prepare_model_query("/goal ship the feature", PREFIX, queue.Queue())
        self.assertIsNotNone(a.active_goal, "goal must be armed even with a prefix")
        self.assertEqual(a.active_goal.condition, "ship the feature")
        self.assertIsNotNone(out)
        self.assertTrue(out.startswith(PREFIX))
        self.assertIn("Stop hook", out)

    def test_bare_goal_arms_goal(self):
        a = _bare_agent()
        a._prepare_model_query("/goal finish", "", queue.Queue())
        self.assertEqual(getattr(a.active_goal, "condition", None), "finish")

    def test_prefix_does_not_mask_local_command(self):
        a = _bare_agent()
        out = a._prepare_model_query("/goal clear", PREFIX, queue.Queue())
        self.assertIsNone(out)

    def test_plain_prompt_gets_prefix(self):
        a = _bare_agent()
        out = a._prepare_model_query("do the thing", PREFIX, queue.Queue())
        self.assertEqual(out, PREFIX + "\n\ndo the thing")

    def test_plain_prompt_without_prefix_is_identity(self):
        a = _bare_agent()
        self.assertEqual(a._prepare_model_query("do the thing", "", queue.Queue()), "do the thing")

    def test_leading_whitespace_goal_still_arms(self):
        # Real bug: the Ink composer can deliver "/goal ..." with leading spaces,
        # and a model_prefix is prepended, yielding "...\n\n /goal ...". The old
        # startswith('/') check then missed the command entirely.
        for lead in (" ", "  ", "\n", "\t "):
            a = _bare_agent()
            out = a._prepare_model_query(lead + "/goal ship it", PREFIX, queue.Queue())
            self.assertEqual(
                getattr(a.active_goal, "condition", None), "ship it",
                "leading %r must still arm the goal" % lead,
            )
            self.assertTrue(out.startswith(PREFIX))


if __name__ == "__main__":
    unittest.main()
