import copy
import unittest

from genericagent.core.projection import (
    MAX_PROJECTION_MESSAGES,
    build_backend_history,
    build_ui_messages,
)


def msg(uuid_value, role, text, *, turn_id=1, origin=None):
    event = {
        "version": 2,
        "type": "message",
        "uuid": uuid_value,
        "turn_id": turn_id,
        "role": role,
        "blocks": [{"type": "text", "text": text}],
        "text": text,
    }
    if origin:
        event["origin"] = origin
    return event


class ProjectionTest(unittest.TestCase):
    def test_backend_projection_is_a_copy_and_never_mutates_events(self):
        events = [msg("u1", "user", "hello"), msg("a1", "assistant", "hi")]
        snapshot = copy.deepcopy(events)

        projected = build_backend_history(events)
        projected[0]["content"][0]["text"] = "changed"

        self.assertEqual(snapshot, events)
        self.assertEqual("hello", events[0]["blocks"][0]["text"])

    def test_duplicate_uuid_is_first_wins(self):
        events = [msg("same", "user", "first"), msg("same", "user", "second")]
        projected = build_backend_history(events)
        self.assertEqual(1, len(projected))
        self.assertEqual("first", projected[0]["content"][0]["text"])

    def test_missing_tool_result_is_explicitly_repaired(self):
        events = [
            msg("u1", "user", "run it"),
            {
                "version": 2,
                "type": "message",
                "uuid": "a1",
                "turn_id": 1,
                "role": "assistant",
                "blocks": [{"type": "tool_use", "id": "call_1", "name": "file_read", "input": {}}],
            },
        ]
        projected = build_backend_history(events)
        self.assertEqual("user", projected[-1]["role"])
        self.assertEqual("tool_result", projected[-1]["content"][0]["type"])
        self.assertIn("missing", projected[-1]["content"][0]["content"])

    def test_orphan_tool_result_is_demoted_to_text(self):
        events = [
            msg("u1", "user", "start"),
            msg("u2", "user", "orphan", origin={"kind": "tool_result"}),
        ]
        events[1]["blocks"] = [{"type": "tool_result", "tool_use_id": "missing", "content": "raw"}]
        projected = build_backend_history(events)
        self.assertEqual("text", projected[-1]["content"][0]["type"])
        self.assertEqual("raw", projected[-1]["content"][0]["text"])

    def test_window_does_not_start_with_tool_result(self):
        events = [msg(f"u{i}", "user", f"question {i}", turn_id=i) for i in range(1, 8)]
        projected = build_backend_history(events, max_messages=3)
        self.assertLessEqual(len(projected), 3)
        self.assertEqual("user", projected[0]["role"])
        self.assertFalse(any(
            block.get("type") == "tool_result"
            for block in projected[0]["content"]
        ))

    def test_rewind_filters_abandoned_turns_from_api_projection(self):
        events = [
            msg("u1", "user", "keep", turn_id=1),
            msg("a1", "assistant", "keep answer", turn_id=1),
            msg("u2", "user", "discard", turn_id=2),
            msg("a2", "assistant", "discard answer", turn_id=2),
            {
                "version": 2,
                "type": "rewind",
                "turn_id": None,
                "keep_turn_id": 1,
            },
            msg("u3", "user", "new branch", turn_id=2),
            msg("a3", "assistant", "new answer", turn_id=2),
        ]
        projected = build_backend_history(events)
        joined = str(projected)
        self.assertIn("keep", joined)
        self.assertIn("new branch", joined)
        self.assertNotIn("discard", joined)

    def test_compact_projection_keeps_summary_preserved_and_post_boundary_messages(self):
        events = [
            msg("u1", "user", "old question", turn_id=1),
            msg("a1", "assistant", "old answer", turn_id=1),
            {
                "version": 2,
                "type": "compact_boundary",
                "uuid": "b1",
                "parent_uuid": None,
                "logical_parent_uuid": "a1",
                "summary_uuid": "s1",
                "preserved_uuids": ["a1"],
            },
            {
                "version": 2,
                "type": "summary",
                "uuid": "s1",
                "parent_uuid": "b1",
                "text": "summarized context",
            },
            msg("u2", "user", "new question", turn_id=2),
            msg("a2", "assistant", "new answer", turn_id=2),
        ]
        projected = build_backend_history(events)
        rendered = str(projected)
        self.assertIn("summarized context", rendered)
        self.assertIn("old answer", rendered)
        self.assertNotIn("old question", rendered)
        self.assertIn("new question", rendered)
        self.assertIn("new answer", rendered)
        self.assertEqual("user", projected[0]["role"])

    def test_compact_preserved_segment_walks_parent_chain(self):
        first = msg("u1", "user", "keep user")
        second = msg("a1", "assistant", "keep answer")
        second["parent_uuid"] = "u1"
        third = msg("t1", "user", "keep result", origin={"kind": "tool_result"})
        third["parent_uuid"] = "a1"
        third["blocks"] = [{"type": "tool_result", "tool_use_id": "x", "content": "result"}]
        events = [
            first,
            second,
            third,
            {
                "version": 2,
                "type": "compact_boundary",
                "uuid": "b1",
                "summary_uuid": "s1",
                "preserved_segment": {"head_uuid": "u1", "tail_uuid": "t1"},
            },
            {"version": 2, "type": "summary", "uuid": "s1", "text": "summary"},
        ]
        projected = build_backend_history(events)
        rendered = str(projected)
        self.assertIn("keep user", rendered)
        self.assertIn("keep answer", rendered)
        self.assertNotIn("missing tool result", rendered)

    def test_compact_missing_summary_falls_back_to_linear_history(self):
        events = [
            msg("u1", "user", "before"),
            msg("a1", "assistant", "before answer"),
            {
                "version": 2,
                "type": "compact_boundary",
                "uuid": "b1",
                "summary_uuid": "missing",
                "preserved_uuids": ["a1"],
            },
            msg("u2", "user", "after", turn_id=2),
        ]
        projected = build_backend_history(events)
        rendered = str(projected)
        self.assertIn("before", rendered)
        self.assertIn("after", rendered)

    def test_compact_missing_preserved_uuid_is_skipped_without_crashing(self):
        events = [
            msg("u1", "user", "not preserved"),
            {
                "version": 2,
                "type": "compact_boundary",
                "uuid": "b1",
                "summary_uuid": "s1",
                "preserved_uuids": ["missing-uuid"],
            },
            {"version": 2, "type": "summary", "uuid": "s1", "text": "summary"},
            msg("u2", "user", "after", turn_id=2),
        ]
        projected = build_backend_history(events)
        rendered = str(projected)
        self.assertIn("summary", rendered)
        self.assertIn("after", rendered)
        self.assertNotIn("not preserved", rendered)

    def test_compact_summary_is_not_replayed_as_a_user_ui_message(self):
        events = [
            msg("u1", "user", "old", turn_id=1),
            {
                "version": 2,
                "type": "compact_boundary",
                "uuid": "b1",
                "summary_uuid": "s1",
            },
            {"version": 2, "type": "summary", "uuid": "s1", "text": "summary text"},
            msg("u2", "user", "new", turn_id=2),
        ]
        self.assertEqual(
            [{"role": "user", "content": "new"}],
            build_ui_messages(events),
        )

    def test_default_window_constant_matches_cc(self):
        self.assertEqual(4096, MAX_PROJECTION_MESSAGES)

    def test_ui_projection_skips_tool_results_but_keeps_human_and_assistant(self):
        events = [
            msg("u1", "user", "question"),
            msg("a1", "assistant", "answering"),
            msg("t1", "user", "", origin={"kind": "tool_result"}),
        ]
        events[2]["blocks"] = [{"type": "tool_result", "tool_use_id": "x", "content": "result"}]
        projected = build_ui_messages(events)
        self.assertEqual(
            [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "answering"},
            ],
            projected,
        )


if __name__ == "__main__":
    unittest.main()
