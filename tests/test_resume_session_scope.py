"""Child sessions must never enter the /resume namespace.

CC 319 evidence (source coordinates in docs/20261109-resume会话作用域修复.md):

* subagent transcripts are written to ``<projectDir>/<sessionId>/subagents/agent-<id>.jsonl``
  (m0279 ``lht()`` = ``sessionsDir/<session>/subagents``);
* the session list only enumerates *top-level files* ending in ``.jsonl``
  (m0591:33509: ``if (!S.isFile() || !S.name.endsWith(".jsonl")) continue``);
* resume-side history scanning skips ``isSidechain === true`` records (m0860:642).

GA stored every process-backed child's own session next to the user's sessions,
so /resume showed teammate task traffic as if the user had written it.
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from genericagent.core import transcripts as session_transcript


class FakeAgent:
    def __init__(self):
        self.session_id = None
        self.session_path = None
        self.session_turn_id = 0


def _write_session(path, session_id, first_user_text, *, extra_start=None):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    start = {"version": 2, "type": "session_start", "session_id": session_id,
             "created_at": "2026-11-09T00:00:00+00:00", "cwd": str(Path(path).parent),
             "seq": 1, "schema": 2}
    start.update(extra_start or {})
    events = [start]
    if first_user_text is not None:
        events.append({"version": 2, "type": "message", "session_id": session_id, "seq": 2,
                       "turn_id": 1, "role": "user", "text": first_user_text})
        events.append({"version": 2, "type": "message", "session_id": session_id, "seq": 3,
                       "turn_id": 1, "role": "assistant", "text": "done"})
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return str(path)


class SubagentSessionPlacementTest(unittest.TestCase):
    def _identity(self, root):
        project = root / "project"
        project.mkdir(parents=True, exist_ok=True)
        session_data = root / "data" / "projects" / "proj-key" / "session_parent" / "subagents"
        session_data.mkdir(parents=True, exist_ok=True)
        return {
            "original_cwd": str(project),
            "project_root": str(project),
            "launch_cwd": str(project),
            "project_key": "proj-key",
            "data_root": str(root / "data"),
            "layout": "project-v1",
            "parent_session_id": "session_parent",
            "session_data_dir": str(session_data),
            "agent_kind": "subagent",
        }

    def test_identity_from_environment_carries_parent_session(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "project").mkdir(parents=True, exist_ok=True)
            env = {
                "GA_PROJECT_ROOT": str(Path(td) / "project"),
                "GA_DATA_DIR": str(Path(td) / "data"),
                "GA_PARENT_SESSION_ID": "session_parent",
                "GA_SESSION_DATA_DIR": str(Path(td) / "sess" / "session_parent" / "subagents"),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                identity = session_transcript.identity_from_environment()
            self.assertIsNotNone(identity)
            self.assertEqual("session_parent", identity.get("parent_session_id"))
            self.assertEqual("subagent", identity.get("agent_kind"))
            self.assertEqual(env["GA_SESSION_DATA_DIR"], identity.get("session_data_dir"))

    def test_user_identity_has_no_agent_kind(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "project").mkdir(parents=True, exist_ok=True)
            env = {"GA_PROJECT_ROOT": str(Path(td) / "project"),
                   "GA_DATA_DIR": str(Path(td) / "data"),
                   "GA_PARENT_SESSION_ID": ""}
            with mock.patch.dict(os.environ, env, clear=False):
                identity = session_transcript.identity_from_environment()
            self.assertIsNone(identity.get("parent_session_id"))
            self.assertIsNone(identity.get("agent_kind"))

    def test_child_session_is_nested_under_parent_subagents_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            identity = self._identity(root)
            agent = FakeAgent()
            path = session_transcript.ensure_agent_session(agent, identity=identity)
            expected_root = Path(identity["session_data_dir"]) / "sessions"
            self.assertEqual(expected_root.resolve(), Path(path).parent.resolve(),
                             "child session must live under <session>/subagents/sessions/")
            self.assertTrue(Path(path).exists())

    def test_child_session_start_records_parent_and_kind(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            identity = self._identity(root)
            agent = FakeAgent()
            path = session_transcript.ensure_agent_session(agent, identity=identity)
            first = json.loads(Path(path).read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual("subagent", first.get("agent_kind"))
            self.assertEqual("session_parent", first.get("parent_session_id"))

    def test_user_session_start_has_no_agent_kind(self):
        with tempfile.TemporaryDirectory() as td:
            identity = {
                "original_cwd": td, "project_root": td, "launch_cwd": td,
                "project_key": "proj-key", "data_root": str(Path(td) / "data"),
                "layout": "project-v1",
            }
            agent = FakeAgent()
            path = session_transcript.ensure_agent_session(agent, identity=identity)
            first = json.loads(Path(path).read_text(encoding="utf-8").splitlines()[0])
            self.assertNotIn("agent_kind", first)
            self.assertNotIn("parent_session_id", first)


class ResumeListingScopeTest(unittest.TestCase):
    def test_marked_child_session_is_hidden_from_default_listing(self):
        with tempfile.TemporaryDirectory() as td:
            _write_session(Path(td) / "session_child.jsonl", "session_child", "do the thing",
                           extra_start={"agent_kind": "subagent",
                                        "parent_session_id": "session_parent"})
            _write_session(Path(td) / "session_user.jsonl", "session_user", "hello")
            listed = [s.session_id for s in session_transcript.list_sessions(root=td)]
            self.assertEqual(["session_user"], listed)
            everything = [s.session_id for s in
                          session_transcript.list_sessions(root=td, include_subagents=True)]
            self.assertIn("session_child", everything)

    def test_legacy_team_protocol_session_is_hidden_from_default_listing(self):
        with tempfile.TemporaryDirectory() as td:
            _write_session(Path(td) / "session_team.jsonl", "session_team",
                           "[TEAM TASK PROTOCOL]\n你是本团队 teammate \"w1\"")
            _write_session(Path(td) / "session_user.jsonl", "session_user", "hello")
            listed = [s.session_id for s in session_transcript.list_sessions(root=td)]
            self.assertEqual(["session_user"], listed)
            everything = [s.session_id for s in
                          session_transcript.list_sessions(root=td, include_subagents=True)]
            self.assertIn("session_team", everything)

    def test_human_session_mentioning_team_protocol_later_is_kept(self):
        with tempfile.TemporaryDirectory() as td:
            _write_session(Path(td) / "session_user.jsonl", "session_user", "what is a teammate?")
            path = Path(td) / "session_user.jsonl"
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps({"version": 2, "type": "message",
                                     "session_id": "session_user", "seq": 4, "turn_id": 2,
                                     "role": "user",
                                     "text": "[TEAM TASK PROTOCOL] 这个词是什么意思"}) + "\n")
            listed = [s.session_id for s in session_transcript.list_sessions(root=td)]
            self.assertEqual(["session_user"], listed)


class ResumeLegacyVisibilityTest(unittest.TestCase):
    """Older conversations live in the legacy root; /resume must reach them by default.

    ``session_roots`` used to be the project bucket only unless ``--all`` was
    passed, so v1 sessions written before the project layout were invisible in
    the picker (the user's "有的历史 session 我找不到了"). The legacy
    ``model_responses_*.txt`` scan stays opt-in: that part is slow and only
    useful for archaeology.
    """

    def _bridge(self, tmp):
        from frontends.ink.bridge import GenericAgentBridge
        from tests.test_ink_bridge import FakeAgent as BridgeFakeAgent

        agent = BridgeFakeAgent()
        agent.session_id = "session_current"
        agent.project_root = str(Path(tmp) / "project")
        agent.data_root = str(Path(tmp) / "data")
        Path(agent.project_root).mkdir(parents=True, exist_ok=True)
        events = []
        return GenericAgentBridge(agent_factory=lambda: agent, emit=events.append), events

    def test_default_scope_includes_the_legacy_session_root(self):
        from genericagent import paths

        with tempfile.TemporaryDirectory() as tmp:
            bridge, _events = self._bridge(tmp)
            roots = paths.project_roots(project=str(Path(tmp) / "project"),
                                        data_root=str(Path(tmp) / "data"))
            kwargs = bridge._resume_exclusion_kwargs(include_legacy=False)
        self.assertIn(roots.project_data_dir, kwargs["session_roots"])
        self.assertIn(roots.legacy_sessions_dir, kwargs["session_roots"])
        self.assertIsNone(kwargs["legacy_roots"],
                          "the legacy .txt scan must stay opt-in")

    def test_legacy_rows_are_tagged_in_the_default_payload(self):
        from genericagent import paths

        with tempfile.TemporaryDirectory() as tmp:
            bridge, events = self._bridge(tmp)
            roots = paths.project_roots(project=str(Path(tmp) / "project"),
                                        data_root=str(Path(tmp) / "data"))
            rows = [
                (str(roots.project_data_dir / "session_project.jsonl"), 1000.0, "project prompt", 1),
                (str(roots.legacy_sessions_dir / "session_legacy.jsonl"), 900.0, "legacy prompt", 2),
            ]
            with mock.patch("frontends.ink.bridge.continue_list", return_value=rows):
                bridge.list_resume_sessions()
        payload = events[-1]
        self.assertNotIn("all", payload)
        by_id = {item["id"]: item for item in payload["sessions"]}
        self.assertEqual("legacy",
                         by_id[str(roots.legacy_sessions_dir / "session_legacy.jsonl")].get("source"))
        self.assertNotIn("source",
                         by_id[str(roots.project_data_dir / "session_project.jsonl")])


class ChildSessionMigrationTest(unittest.TestCase):
    """Retire the child sessions that were written before placement changed.

    Every process-backed child writes its spawn message to
    ``<project>/<parent>/subagents/tasks/<name>/input.txt`` and starts its own
    transcript with exactly that text. Verified on the real bucket: 109/109
    top-level sessions that look internal match an input.txt byte for byte, and
    none of the 21 remaining sessions matches any of them. That equality is the
    only proof the migration accepts; anything else is left alone and reported.
    """

    def _bucket(self, tmp):
        from genericagent.core import session_scope

        base = Path(tmp)
        child_prompt = '[TEAM TASK PROTOCOL]\n你是本团队 teammate "w1"'
        task_dir = base / "session_parent" / "subagents" / "tasks" / "w1"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "input.txt").write_text(child_prompt, encoding="utf-8", newline="\n")
        child_path = _write_session(base / "session_child.jsonl", "session_child", child_prompt,
                                    extra_start={})
        user_path = _write_session(base / "session_user.jsonl", "session_user", "hello there")
        # Backdate both: a transcript written seconds ago is treated as possibly
        # live and left alone (see test_a_session_written_seconds_ago_is_left_alone).
        old = time.time() - 3600
        os.utime(child_path, (old, old))
        os.utime(user_path, (old, old))
        return session_scope, base

    def test_plan_moves_child_matched_by_task_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            scope, base = self._bucket(tmp)
            plan = scope.plan_child_session_migration(base)
            moved = {Path(m.session_path).name: m.parent_session_id for m in plan.moves}
            self.assertEqual({"session_child.jsonl": "session_parent"}, moved)
            self.assertEqual(["session_user.jsonl"],
                             [Path(p).name for p in plan.kept])

    def test_plan_marks_ambiguous_prompts_instead_of_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            scope, base = self._bucket(tmp)
            dup = base / "session_other" / "subagents" / "tasks" / "w1"
            dup.mkdir(parents=True, exist_ok=True)
            (dup / "input.txt").write_text(
                '[TEAM TASK PROTOCOL]\n你是本团队 teammate "w1"',
                encoding="utf-8", newline="\n")
            plan = scope.plan_child_session_migration(base)
            self.assertEqual([], plan.moves)
            self.assertEqual(["session_child.jsonl"],
                             [Path(p).name for p in plan.ambiguous])

    def test_a_session_written_seconds_ago_is_left_alone(self):
        # A live child keeps appending to its transcript. Moving the file mid-run
        # would split it: the next append recreates the old path. Anything fresh
        # is reported and skipped.
        with tempfile.TemporaryDirectory() as tmp:
            scope, base = self._bucket(tmp)
            fresh = time.time()
            child = base / "session_child.jsonl"
            os.utime(child, (fresh, fresh))
            plan = scope.plan_child_session_migration(base)
            self.assertEqual([], plan.moves)
            self.assertEqual(["session_child.jsonl"],
                             [Path(p).name for p in plan.ambiguous])

    def test_apply_moves_the_file_under_the_parent_and_keeps_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            scope, base = self._bucket(tmp)
            plan = scope.plan_child_session_migration(base)
            applied = scope.apply_child_session_migration(plan)
            self.assertEqual(1, len(applied))
            target = base / "session_parent" / "subagents" / "sessions" / "session_child.jsonl"
            self.assertTrue(target.exists())
            self.assertFalse((base / "session_child.jsonl").exists())
            self.assertEqual(1, applied[0]["moved"])
            # A second run has nothing left to do.
            again = scope.plan_child_session_migration(base)
            self.assertEqual([], again.moves)


if __name__ == "__main__":
    unittest.main()
