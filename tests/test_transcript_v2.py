import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from genericagent.core import transcripts as session_transcript


class FakeBackend:
    def __init__(self):
        self.history = []


class FakeClient:
    def __init__(self):
        self.backend = FakeBackend()
        self.last_tools = ""


class FakeAgent:
    def __init__(self):
        self.history = []
        self.handler = None
        self.llmclient = FakeClient()

    def abort(self):
        pass


class TranscriptV2Test(unittest.TestCase):
    def test_new_sessions_default_to_v2_and_keep_the_rollback_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(root=tmp, cwd="C:/repo")
            first = json.loads(Path(path).read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(2, first["version"])
            self.assertEqual(2, first["schema"])

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"GA_TRANSCRIPT_SCHEMA": "v1"}):
            path = session_transcript.create_session(root=tmp, cwd="C:/repo")
            first = json.loads(Path(path).read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(1, first["version"])

    def test_record_agent_turn_writes_only_this_turn_delta_and_turn_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(
                root=tmp, cwd="C:/repo", session_id="session_v2"
            )
            agent = FakeAgent()
            agent.session_id = "session_v2"
            agent.session_path = path
            agent.session_turn_id = 0
            before = [
                {"role": "user", "content": [{"type": "text", "text": "old"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]},
            ]
            after = before + [
                {"role": "user", "content": [{"type": "text", "text": "new"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "new answer"}]},
            ]
            agent.llmclient.backend.history = after

            session_transcript.record_agent_turn(
                agent,
                user_text="new",
                assistant_text="new answer",
                source="user",
                backend_history_before=before,
            )

            events = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
            kinds = [event["type"] for event in events]
            self.assertEqual(["session_start", "message", "message", "turn_end", "working_state"], kinds)
            serialized = json.dumps(events, ensure_ascii=False)
            self.assertNotIn("backend_history_before", serialized)
            self.assertNotIn("backend_history_after", serialized)
            self.assertTrue(events[-1]["handler_working"] == {})
            self.assertEqual("", events[-1]["last_tools"])
            self.assertEqual(["new", "new answer"], [event.get("text") for event in events[1:3]])
            loaded = session_transcript.load_session(path)
            self.assertEqual(after[2:], loaded.backend_history)
            self.assertEqual(["new"], [turn.user_text for turn in loaded.turns])

    def test_record_agent_turn_accepts_a_history_length_as_the_v2_delta_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(
                root=tmp, cwd="C:/repo", session_id="session_v2_delta"
            )
            agent = FakeAgent()
            agent.session_id = "session_v2_delta"
            agent.session_path = path
            agent.session_turn_id = 0
            old = [
                {"role": "user", "content": [{"type": "text", "text": "old"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]},
            ]
            agent.llmclient.backend.history = old + [
                {"role": "user", "content": [{"type": "text", "text": "new"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "new answer"}]},
            ]

            with patch.object(
                session_transcript,
                "current_backend_history",
                side_effect=AssertionError("integer anchor must not copy full history"),
            ):
                session_transcript.record_agent_turn(
                    agent,
                    user_text="new",
                    assistant_text="new answer",
                    source="user",
                    backend_history_before=len(old),
                )

            events = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(["new", "new answer"], [event.get("text") for event in events if event["type"] == "message"])

    def test_v2_reader_round_trips_ui_backend_and_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(
                root=tmp, cwd="C:/repo", session_id="session_list"
            )
            agent = FakeAgent()
            agent.session_id = "session_list"
            agent.session_path = path
            agent.session_turn_id = 0
            agent.llmclient.backend.history = [
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            ]
            session_transcript.record_agent_turn(
                agent,
                user_text="hello",
                assistant_text="hi",
                source="user",
                backend_history_before=[],
            )

            loaded = session_transcript.load_session(path)
            summary = session_transcript.list_sessions(root=tmp)[0]
            self.assertEqual(1, loaded.rounds)
            self.assertEqual("hello", loaded.preview)
            self.assertEqual(
                [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
                loaded.ui_messages,
            )
            self.assertEqual(1, summary.rounds)
            self.assertEqual("hello", summary.preview)

    def test_duplicate_uuid_and_malformed_line_are_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_dup")
            session_transcript.append_message(
                path,
                session_id="session_dup",
                role="user",
                blocks=[{"type": "text", "text": "one"}],
                origin={"kind": "human"},
                turn_id=1,
                text="one",
                uuid_value="same-uuid",
            )
            session_transcript.append_message(
                path,
                session_id="session_dup",
                role="user",
                blocks=[{"type": "text", "text": "duplicate"}],
                origin={"kind": "human"},
                turn_id=1,
                text="duplicate",
                uuid_value="same-uuid",
            )
            session_transcript.append_turn_end(
                path,
                session_id="session_dup",
                turn_id=1,
                first_uuid="same-uuid",
                last_uuid="same-uuid",
            )
            with Path(path).open("a", encoding="utf-8", newline="\n") as fh:
                fh.write("{broken json\n")

            loaded = session_transcript.load_session(path)
            self.assertEqual(1, loaded.rounds)
            self.assertEqual(1, len(loaded.backend_history))
            self.assertEqual("one", loaded.ui_messages[0]["content"])
            self.assertTrue(loaded.warnings)


    def test_compact_v2_writes_boundary_and_summary_without_history_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(
                root=tmp, cwd="C:/repo", session_id="session_compact_v2"
            )
            session_transcript.append_message(
                path,
                session_id="session_compact_v2",
                role="user",
                blocks=[{"type": "text", "text": "keep this turn"}],
                origin={"kind": "human"},
                turn_id=1,
                text="keep this turn",
                uuid_value="u1",
            )
            session_transcript.append_message(
                path,
                session_id="session_compact_v2",
                role="assistant",
                blocks=[{"type": "text", "text": "keep this answer"}],
                origin={"kind": "assistant"},
                turn_id=1,
                text="keep this answer",
                uuid_value="a1",
            )
            session_transcript.record_compact_v2(
                path,
                session_id="session_compact_v2",
                summary="durable compact summary",
                trigger="manual",
                messages_summarized=2,
            )
            session_transcript.append_message(
                path,
                session_id="session_compact_v2",
                role="user",
                blocks=[{"type": "text", "text": "after boundary"}],
                origin={"kind": "human"},
                turn_id=2,
                text="after boundary",
                uuid_value="u2",
            )
            session_transcript.append_turn_end(
                path,
                session_id="session_compact_v2",
                turn_id=2,
                first_uuid="u2",
                last_uuid="u2",
            )

            raw_lines = Path(path).read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in raw_lines]
            self.assertEqual(
                ["session_start", "message", "message", "compact_boundary", "summary", "message", "turn_end"],
                [event["type"] for event in events],
            )
            self.assertFalse(any("backend_history_after" in line for line in raw_lines))
            boundary = next(event for event in events if event["type"] == "compact_boundary")
            self.assertIsNone(boundary["parent_uuid"])
            self.assertEqual("a1", boundary["logical_parent_uuid"])
            self.assertEqual(["u1", "a1"], boundary["preserved_uuids"])
            self.assertEqual(
                "durable compact summary",
                next(event["text"] for event in events if event["type"] == "summary"),
            )

            loaded = session_transcript.load_session(path)
            self.assertEqual(["after boundary"], [turn.user_text for turn in loaded.turns])
            rendered = str(loaded.backend_history)
            self.assertIn("durable compact summary", rendered)
            self.assertIn("keep this turn", rendered)
            self.assertIn("after boundary", rendered)
            self.assertEqual(
                [{"role": "user", "content": "after boundary"}],
                loaded.ui_messages,
            )

    def test_compact_v2_missing_summary_falls_back_safely_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(
                root=tmp, cwd="C:/repo", session_id="session_bad_compact"
            )
            session_transcript.append_message(
                path,
                session_id="session_bad_compact",
                role="user",
                blocks=[{"type": "text", "text": "survives"}],
                origin={"kind": "human"},
                turn_id=1,
                text="survives",
                uuid_value="u1",
            )
            session_transcript.append_compact_boundary(
                path,
                session_id="session_bad_compact",
                trigger="manual",
                summary_uuid="missing-summary",
                logical_parent_uuid="u1",
                preserved_uuids=["u1"],
            )
            loaded = session_transcript.load_session(path)
            self.assertIn("survives", str(loaded.backend_history))

    def test_rewind_anchor_projection_restores_exact_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(
                root=tmp, cwd="C:/repo", session_id="session_anchor"
            )
            session_transcript.append_message(
                path, session_id="session_anchor", role="user",
                blocks=[{"type": "text", "text": "one"}],
                origin={"kind": "human"}, turn_id=1, text="one", uuid_value="u1",
            )
            session_transcript.append_message(
                path, session_id="session_anchor", role="assistant",
                blocks=[{"type": "text", "text": "a1"}],
                origin={"kind": "assistant"}, turn_id=1, text="a1", uuid_value="a1",
            )
            session_transcript.append_message(
                path, session_id="session_anchor", role="user",
                blocks=[{"type": "text", "text": "two"}],
                origin={"kind": "human"}, turn_id=2, text="two", uuid_value="u2",
            )
            session_transcript.append_message(
                path, session_id="session_anchor", role="assistant",
                blocks=[{"type": "text", "text": "a2"}],
                origin={"kind": "assistant"}, turn_id=2, text="a2", uuid_value="a2",
            )
            session_transcript.record_rewind_v2(
                path, session_id="session_anchor", keep_turn_id=1,
                anchor_uuid="a1", head_uuid="u2", tail_uuid="a2",
            )
            session_transcript.append_message(
                path, session_id="session_anchor", role="user",
                blocks=[{"type": "text", "text": "new two"}],
                origin={"kind": "human"}, turn_id=2, text="new two", uuid_value="u3",
            )

            projected = session_transcript.load_history_at_anchor(
                path, anchor_uuid="a1", keep_turn_id=1
            )
            rendered = str(projected)
            self.assertIn("one", rendered)
            self.assertIn("a1", rendered)
            self.assertIsNone(session_transcript.load_history_at_anchor(
                path, anchor_uuid="missing", keep_turn_id=1
            ))
            loaded = session_transcript.load_session(path)
            loaded_texts = [
                block.get("text")
                for message in loaded.backend_history
                for block in message.get("content", [])
                if isinstance(block, dict)
            ]
            self.assertNotIn("two", loaded_texts)
            self.assertIn("new two", loaded_texts)
            rewind_event = next(
                event for event in loaded.events if event["type"] == "rewind"
            )
            self.assertNotIn("backend_history_after", rewind_event)

    def test_v1_branch_turn_keeps_anchor_metadata_after_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = [
                {'role': 'user', 'content': [{'type': 'text', 'text': 'old question'}]},
                {'role': 'assistant', 'content': [{'type': 'text', 'text': 'old answer'}]},
            ]
            legacy = session_transcript.create_session(
                root=tmp, cwd='C:/repo', session_id='session_legacy_anchor', schema=1
            )
            session_transcript.record_turn(
                legacy,
                session_id='session_legacy_anchor',
                turn_id=1,
                source='user',
                user_text='old question',
                assistant_text='old answer',
                backend_history_before=[],
                backend_history_after=history,
            )
            agent = FakeAgent()
            session_transcript.restore_agent_session(agent, legacy)
            agent.llmclient.backend.history = history + [
                {'role': 'user', 'content': [{'type': 'text', 'text': 'new question'}]},
                {'role': 'assistant', 'content': [{'type': 'text', 'text': 'new answer'}]},
            ]
            session_transcript.record_agent_turn(
                agent,
                user_text='new question',
                assistant_text='new answer',
                source='user',
                backend_history_before=history,
            )

            loaded = session_transcript.load_session(agent.session_path)

            self.assertEqual('', loaded.turns[0].anchor_uuid)
            self.assertTrue(loaded.turns[1].anchor_uuid)
            self.assertTrue(loaded.turns[1].last_uuid)

    def test_working_state_round_trip_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(
                root=tmp, cwd='C:/repo', session_id='session_working'
            )
            session_transcript.append_working_state(
                path,
                session_id='session_working',
                handler_working={
                    'key_info': 'remember the branch',
                    'active_skill': 'demo',
                    'active_skill_allowed_tools': 'file_read, code_run',
                    'related_sop': 'demo.md',
                    '_loaded_skill_records': {
                        'demo': {
                            'name': 'demo',
                            'description': 'demo skill',
                            'base_dir': 'C:/skills/demo',
                            'content_chars': 12,
                            'state': 'on',
                        }
                    },
                    '_loaded_skill_hashes': {'demo': 'hash'},
                    'temporary_large_object': {'drop': True},
                },
                last_tools='tools-json',
                last_usage_tokens={'input_tokens': 3, 'output_tokens': 2},
            )
            loaded = session_transcript.load_session(path)
            self.assertIsNotNone(loaded.working_state)
            self.assertEqual(
                'remember the branch',
                loaded.working_state['handler_working']['key_info'],
            )
            self.assertNotIn('temporary_large_object', loaded.working_state['handler_working'])
            self.assertEqual('tools-json', loaded.working_state['last_tools'])
            self.assertEqual(3, loaded.working_state['last_usage_tokens']['input_tokens'])

            agent = FakeAgent()
            restored = session_transcript.restore_agent_session(agent, path)
            self.assertTrue(restored.ok)
            self.assertEqual('tools-json', agent.llmclient.last_tools)
            self.assertEqual(
                {'input_tokens': 3, 'output_tokens': 2},
                agent.llmclient.backend.last_usage_tokens,
            )
            self.assertEqual(
                'remember the branch',
                agent._resumed_working_state['handler_working']['key_info'],
            )

    def test_working_state_is_keyed_by_turn_and_restores_the_requested_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(
                root=tmp, cwd='C:/repo', session_id='session_working_turns'
            )
            for turn_id, marker in ((1, 'first-state'), (2, 'second-state')):
                session_transcript.append_message(
                    path, session_id='session_working_turns', role='user',
                    blocks=[{'type': 'text', 'text': f'turn {turn_id}'}],
                    origin={'kind': 'human'}, turn_id=turn_id,
                    text=f'turn {turn_id}', uuid_value=f'u{turn_id}',
                )
                session_transcript.append_turn_end(
                    path, session_id='session_working_turns', turn_id=turn_id,
                    first_uuid=f'u{turn_id}', last_uuid=f'u{turn_id}',
                )
                session_transcript.append_working_state(
                    path, session_id='session_working_turns',
                    handler_working={'key_info': marker},
                    last_tools=f'tools-{turn_id}', turn_id=turn_id,
                )
            loaded = session_transcript.load_session(path)
            self.assertEqual('first-state', loaded.working_states[1]['handler_working']['key_info'])
            self.assertEqual('second-state', loaded.working_states[2]['handler_working']['key_info'])
            self.assertEqual('second-state', loaded.working_state['handler_working']['key_info'])

    def test_compact_v2_embeds_precompact_working_state_for_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(
                root=tmp, cwd='C:/repo', session_id='session_compact_working'
            )
            session_transcript.append_message(
                path, session_id='session_compact_working', role='user',
                blocks=[{'type': 'text', 'text': 'old'}],
                origin={'kind': 'human'}, turn_id=1, text='old', uuid_value='u1',
            )
            session_transcript.record_compact_v2(
                path,
                session_id='session_compact_working',
                summary='summary',
                turn_id=1,
                preserved_uuids=['u1'],
                working_state={
                    'handler_working': {'key_info': 'keep-me'},
                    'last_tools': 'tools-json',
                    'last_usage_tokens': {'input_tokens': 7},
                },
            )
            loaded = session_transcript.load_session(path)
            self.assertEqual('keep-me', loaded.working_state['handler_working']['key_info'])
            self.assertEqual('tools-json', loaded.working_state['last_tools'])
            self.assertEqual(7, loaded.working_state['last_usage_tokens']['input_tokens'])

    def test_rewind_event_embeds_restored_lightweight_working_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(
                root=tmp, cwd='C:/repo', session_id='session_rewind_working'
            )
            session_transcript.record_rewind_v2(
                path,
                session_id='session_rewind_working',
                keep_turn_id=0,
                anchor_uuid='u0',
                working_state={
                    'handler_working': {'key_info': 'before'},
                    'last_tools': 'tools-before',
                },
            )
            loaded = session_transcript.load_session(path)
            rewind = next(event for event in loaded.events if event['type'] == 'rewind')
            self.assertEqual('before', rewind['working_state']['handler_working']['key_info'])
            self.assertEqual('tools-before', rewind['working_state']['last_tools'])

    def test_v2_compact_and_rewind_snapshot_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_compat")
            agent = FakeAgent()
            agent.session_id = "session_compat"
            agent.session_path = path
            agent.session_turn_id = 0
            first = [
                {"role": "user", "content": [{"type": "text", "text": "one"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
            ]
            agent.llmclient.backend.history = first
            session_transcript.record_agent_turn(
                agent, user_text="one", assistant_text="a1", source="user", backend_history_before=[]
            )
            compacted = [{"role": "user", "content": [{"type": "text", "text": "summary"}]}]
            session_transcript.record_compact(
                path, session_id="session_compat", message="summary", backend_history_after=compacted
            )
            loaded = session_transcript.load_session(path)
            self.assertEqual(compacted, loaded.backend_history)

            session_transcript.record_rewind(
                path, session_id="session_compat", keep_turns=1, backend_history_after=first
            )
            loaded = session_transcript.load_session(path)
            self.assertEqual(first, loaded.backend_history)

    def test_v1_remains_readable_and_v1_round_trip_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(
                root=tmp, cwd="C:/repo", session_id="session_v1", schema=1
            )
            after = [{"role": "user", "content": "legacy"}]
            session_transcript.record_turn(
                path,
                session_id="session_v1",
                turn_id=1,
                source="user",
                user_text="legacy",
                assistant_text="answer",
                backend_history_before=[],
                backend_history_after=after,
            )
            loaded = session_transcript.load_session(path)
            self.assertEqual(1, loaded.rounds)
            self.assertEqual(after, loaded.backend_history)

    def test_v2_resume_projects_tool_pairs_and_continues_with_delta_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(
                root=tmp, cwd="C:/repo", session_id="session_tools"
            )
            session_transcript.append_message(
                path,
                session_id="session_tools",
                role="user",
                blocks=[{"type": "text", "text": "read it"}],
                origin={"kind": "human"},
                turn_id=1,
                text="read it",
            )
            session_transcript.append_message(
                path,
                session_id="session_tools",
                role="assistant",
                blocks=[{"type": "text", "text": "checking"},
                        {"type": "tool_use", "id": "call_1", "name": "file_read", "input": {"path": "x"}}],
                origin={"kind": "assistant"},
                turn_id=1,
            )
            session_transcript.append_message(
                path,
                session_id="session_tools",
                role="user",
                blocks=[{"type": "tool_result", "tool_use_id": "call_1", "content": "file body"}],
                origin={"kind": "tool_result"},
                turn_id=1,
            )
            session_transcript.append_message(
                path,
                session_id="session_tools",
                role="assistant",
                blocks=[{"type": "text", "text": "done"}],
                origin={"kind": "assistant"},
                turn_id=1,
                text="done",
            )
            session_transcript.append_turn_end(
                path, session_id="session_tools", turn_id=1,
                first_uuid="first", last_uuid="last",
            )
            agent = FakeAgent()

            restored = session_transcript.restore_agent_session(agent, path)

            self.assertTrue(restored.ok)
            self.assertEqual(
                [
                    {"role": "user", "content": "read it"},
                    {"role": "assistant", "content": "done"},
                ],
                restored.session.ui_messages,
            )
            projected = restored.session.backend_history
            self.assertEqual(["user", "assistant", "user", "assistant"], [item["role"] for item in projected])
            self.assertEqual("tool_use", projected[1]["content"][-1]["type"])
            self.assertEqual("tool_result", projected[2]["content"][0]["type"])

            before = copy.deepcopy(projected)
            agent.llmclient.backend.history = before + [
                {"role": "user", "content": [{"type": "text", "text": "next"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "next answer"}]},
            ]
            agent.session_turn_id = restored.session.branch_rounds
            session_transcript.record_agent_turn(
                agent,
                user_text="next",
                assistant_text="next answer",
                source="user",
                backend_history_before=before,
            )
            events = [
                json.loads(line)
                for line in Path(path).read_text(encoding="utf-8").splitlines()
            ]
            new_messages = [event for event in events if event["type"] == "message" and event.get("turn_id") == 2]
            self.assertEqual(["next", "next answer"], [event.get("text") for event in new_messages])
            self.assertFalse(any("backend_history_" in json.dumps(event) for event in events))

    def test_resuming_v1_branches_to_v2_without_touching_the_legacy_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = session_transcript.create_session(
                root=tmp, cwd="C:/repo", session_id="session_legacy", schema=1
            )
            legacy_history = [
                {"role": "user", "content": [{"type": "text", "text": "old question"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]},
            ]
            session_transcript.record_turn(
                legacy,
                session_id="session_legacy",
                turn_id=1,
                source="user",
                user_text="old question",
                assistant_text="old answer",
                backend_history_before=[],
                backend_history_after=legacy_history,
            )
            legacy_bytes = Path(legacy).read_bytes()
            agent = FakeAgent()

            restored = session_transcript.restore_agent_session(agent, legacy)

            self.assertTrue(restored.ok)
            self.assertTrue(agent._resumed_from_v1)
            self.assertEqual(legacy_history, agent.llmclient.backend.history)

            agent.llmclient.backend.history = legacy_history + [
                {"role": "user", "content": [{"type": "text", "text": "new question"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "new answer"}]},
            ]
            session_transcript.record_agent_turn(
                agent,
                user_text="new question",
                assistant_text="new answer",
                source="user",
                backend_history_before=legacy_history,
            )

            self.assertEqual(legacy_bytes, Path(legacy).read_bytes())
            self.assertNotEqual(legacy, agent.session_path)
            self.assertTrue(agent.session_path.endswith(".jsonl"))
            events = [
                json.loads(line)
                for line in Path(agent.session_path).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(2, events[0]["version"])
            self.assertEqual("resumed_from", events[1]["type"])
            self.assertEqual(legacy, events[1]["source_path"])
            self.assertEqual(1, agent.session_turn_id)
            loaded = session_transcript.load_session(agent.session_path)
            self.assertEqual(2, loaded.rounds)
            self.assertEqual(1, loaded.branch_rounds)
            self.assertEqual(["old question", "new question"], [turn.user_text for turn in loaded.turns])
            self.assertEqual(["old question", "new question"], [
                content
                for message in loaded.ui_messages
                if message["role"] == "user"
                for content in [message["content"]]
            ])

    def test_v2_record_turn_rejects_accidental_v1_snapshot_mix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_v2")
            with self.assertRaises(ValueError):
                session_transcript.record_turn(
                    path,
                    session_id="session_v2",
                    turn_id=1,
                    source="user",
                    user_text="x",
                    assistant_text="y",
                    backend_history_before=[],
                    backend_history_after=[],
                )


if __name__ == "__main__":
    unittest.main()
