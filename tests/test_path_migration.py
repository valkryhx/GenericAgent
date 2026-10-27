import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ga_cli import migrate_paths
from genericagent import paths


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PathMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.data = self.root / "data"
        self.project.mkdir()
        self.roots = paths.project_roots(project=self.project, data_root=self.data)

    def make_legacy_session(self, name="session_legacy.jsonl", payload=b"legacy"):
        source_dir = self.roots.legacy_sessions_dir
        source_dir.mkdir(parents=True, exist_ok=True)
        source = source_dir / name
        source.write_bytes(payload)
        return source

    def test_dry_run_does_not_write_target(self):
        source = self.make_legacy_session()
        result = migrate_paths.run_migration(self.project, self.data, kinds={"sessions"})
        self.assertEqual("would_copy", result["items"][0]["status"])
        self.assertFalse((self.roots.project_data_dir / source.name).exists())

    def test_apply_copies_without_deleting_source(self):
        source = self.make_legacy_session()
        before = digest(source)
        result = migrate_paths.run_migration(self.project, self.data, apply=True, kinds={"sessions"})
        target = self.roots.project_data_dir / source.name
        self.assertEqual("copied", result["items"][0]["status"])
        self.assertEqual(before, digest(source))
        self.assertEqual(before, digest(target))

    def test_same_hash_is_idempotent_and_different_bytes_conflict(self):
        source = self.make_legacy_session()
        target = self.roots.project_data_dir / source.name
        target.parent.mkdir(parents=True)
        target.write_bytes(source.read_bytes())
        result = migrate_paths.run_migration(self.project, self.data, apply=True, kinds={"sessions"})
        self.assertEqual("already_present", result["items"][0]["status"])

        target.write_bytes(b"different")
        result = migrate_paths.run_migration(self.project, self.data, apply=True, kinds={"sessions"})
        self.assertEqual("conflict", result["items"][0]["status"])
        self.assertEqual(b"different", target.read_bytes())

    def test_session_attachments_are_copied_recursively(self):
        session = self.make_legacy_session()
        session_dir = self.roots.legacy_sessions_dir / session.stem
        files = {
            session_dir / "subagents" / "agent_1" / "registry.json": bytes([123]) + b'"agent":1' + bytes([125, 10]),
            session_dir / "subagents" / "agent_1" / "transcript.jsonl": bytes([123]) + b'"type":"start"' + bytes([125, 10]),
            session_dir / "workflows" / "run_1" / "state.json": bytes([123]) + b'"run":"run_1"' + bytes([125, 10]),
            session_dir / "workflows" / "run_1" / "journal.jsonl": bytes([123]) + b'"step":1' + bytes([125, 10]),
        }
        for file_path, payload in files.items():
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(payload)

        result = migrate_paths.run_migration(
            self.project, self.data, apply=True, kinds={"sessions"}
        )
        statuses = {
            Path(item["source"]).relative_to(self.roots.legacy_sessions_dir).as_posix(): item["status"]
            for item in result["items"]
        }
        self.assertEqual("copied", statuses[session.name])
        for source, payload in files.items():
            relative = source.relative_to(self.roots.legacy_sessions_dir)
            target = self.roots.project_data_dir / relative
            self.assertEqual(payload, target.read_bytes())
            self.assertEqual(payload, source.read_bytes())

    def test_workflow_run_copies_complete_artifact_tree(self):
        run_dir = self.roots.legacy_sessions_dir / "session_demo" / "workflows" / "run_1"
        files = {
            run_dir / "state.json": bytes([123]) + b'"status":"completed"' + bytes([125, 10]),
            run_dir / "script.js": b'export default 1;' + bytes([10]),
            run_dir / "journal.jsonl": bytes([123]) + b'"event":"done"' + bytes([125, 10]),
            run_dir / "agents" / "agent_1" / "result.json": bytes([123]) + b'"ok":true' + bytes([125, 10]),
            run_dir / "agents" / "agent_1" / "transcript.jsonl": bytes([123]) + b'"type":"agent"' + bytes([125, 10]),
            run_dir / "workflow-progress.json": bytes([123]) + b'"jobs":[]' + bytes([125, 10]),
            run_dir / "final-result.json": bytes([123]) + b'"result":1' + bytes([125, 10]),
        }
        for file_path, payload in files.items():
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(payload)

        result = migrate_paths.run_migration(
            self.project, self.data, apply=True, kinds={"workflows"}
        )
        self.assertTrue(result["items"])
        self.assertTrue(all(item["status"] == "copied" for item in result["items"]))
        target_dir = self.roots.project_data_dir / "session_demo" / "workflows" / "run_1"
        for source, payload in files.items():
            target = target_dir / source.relative_to(run_dir)
            self.assertEqual(payload, target.read_bytes())
            self.assertEqual(payload, source.read_bytes())

    def test_tree_conflict_blocks_entire_tree_without_partial_copy(self):
        run_dir = self.roots.legacy_sessions_dir / "session_demo" / "workflows" / "run_1"
        (run_dir / "agents" / "agent_1").mkdir(parents=True)
        (run_dir / "state.json").write_bytes(bytes([123]) + b'"ok":1' + bytes([125, 10]))
        (run_dir / "agents" / "agent_1" / "result.json").write_bytes(bytes([123]) + b'"ok":true' + bytes([125, 10]))
        conflict = self.roots.project_data_dir / "session_demo" / "workflows" / "run_1" / "agents" / "agent_1" / "result.json"
        conflict.parent.mkdir(parents=True)
        conflict.write_bytes(bytes([123]) + b'"ok":false' + bytes([125, 10]))

        result = migrate_paths.run_migration(
            self.project, self.data, apply=True, kinds={"workflows"}
        )
        statuses = [item["status"] for item in result["items"]]
        self.assertIn("conflict", statuses)
        self.assertIn("blocked", statuses)
        self.assertTrue(result["apply_blocked"])
        self.assertEqual(bytes([123]) + b'"ok":false' + bytes([125, 10]), conflict.read_bytes())
        planned_state = self.roots.project_data_dir / "session_demo" / "workflows" / "run_1" / "state.json"
        self.assertFalse(planned_state.exists())

    def test_named_workflow_scope_targets(self):
        source_dir = self.roots.legacy_workflows_dir
        source_dir.mkdir(parents=True)
        source = source_dir / "demo.json"
        source.write_text(json.dumps({"name": "demo", "script": "return 1"}), encoding="utf-8")
        project = migrate_paths.run_migration(self.project, self.data, apply=True, kinds={"named"})
        self.assertEqual("copied", project["items"][0]["status"])
        self.assertEqual(source.read_bytes(), (self.project / ".ga" / "workflows" / "demo.json").read_bytes())

    def test_claim_dry_run_and_apply_never_modify_jsonl(self):
        source = self.make_legacy_session(payload=b'{"version":1}\n')
        before = digest(source)
        dry = migrate_paths.claim_legacy_session(source, project_root=self.project, data_root=self.data)
        self.assertEqual("would_claim", dry.status)
        self.assertFalse(Path(dry.target).exists())
        applied = migrate_paths.claim_legacy_session(source, project_root=self.project, data_root=self.data, apply=True)
        self.assertEqual("claimed", applied.status)
        self.assertEqual(before, digest(source))
        payload = json.loads(Path(applied.target).read_text(encoding="utf-8"))
        self.assertEqual(before, payload["source_sha256"])
        again = migrate_paths.claim_legacy_session(source, project_root=self.project, data_root=self.data, apply=True)
        self.assertEqual("already_present", again.status)

    def test_dry_run_is_repeatable_without_partial_writes(self):
        self.make_legacy_session(payload=b"dry")
        for _ in range(2):
            result = migrate_paths.run_migration(
                self.project, self.data, apply=False, kinds={"sessions"}
            )
            self.assertEqual("dry-run", result["mode"])
            self.assertEqual("would_copy", result["items"][0]["status"])
            self.assertFalse((self.roots.project_data_dir / "session_legacy.jsonl").exists())
        applied = migrate_paths.run_migration(
            self.project, self.data, apply=True, kinds={"sessions"}
        )
        self.assertEqual("copied", applied["items"][0]["status"])
        self.assertNotIn("already_applied", applied)
        rerun = migrate_paths.run_migration(
            self.project, self.data, apply=True, kinds={"sessions"}
        )
        self.assertEqual("already_present", rerun["items"][0]["status"])
        self.assertIn("previous_apply", rerun)
        third = migrate_paths.run_migration(
            self.project, self.data, apply=True, kinds={"sessions"}
        )
        self.assertEqual("already_present", third["items"][0]["status"])
        self.assertTrue((self.roots.project_data_dir / "session_legacy.jsonl").is_file())

    def test_team_boards_require_explicit_mapping_and_never_guess(self):
        legacy = self.data / "temp" / "teams"
        board = legacy / "old-team" / "tasks.json"
        board.parent.mkdir(parents=True)
        board.write_bytes(bytes([123]) + b'"tasks":[]' + bytes([125, 10]))

        unmapped = migrate_paths.run_migration(
            self.project, self.data, apply=True, kinds={"teams"}
        )
        skipped = [item for item in unmapped["items"] if item["kind"] == "team_task"]
        self.assertEqual(1, len(skipped))
        self.assertEqual("skipped", skipped[0]["status"])
        self.assertIn("team-map", skipped[0]["reason"])
        self.assertFalse((self.data / "tasks").exists())

        mapped = migrate_paths.run_migration(
            self.project,
            self.data,
            apply=True,
            kinds={"teams"},
            team_map={"old-team": "test-project--demo"},
        )
        copied = [item for item in mapped["items"] if item["status"] == "copied"]
        self.assertEqual(1, len(copied))
        target = self.data / "tasks" / "test-project--demo" / "tasks.json"
        self.assertEqual(board.read_bytes(), target.read_bytes())
        self.assertEqual(board.read_bytes(), board.read_bytes())

    def test_report_contains_operator_hashes_and_source_scope(self):
        self.make_legacy_session()
        result = migrate_paths.run_migration(
            self.project,
            self.data,
            kinds={"sessions"},
            report=True,
            operator="test-operator",
        )
        self.assertEqual("test-operator", result["operator"])
        self.assertIn("timestamp_utc", result)
        self.assertEqual("dry-run", result["mode"])
        self.assertEqual("would_copy", result["items"][0]["status"])
        self.assertEqual(64, len(result["items"][0]["sha256"]))
        report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
        self.assertEqual("test-operator", report["operator"])
        self.assertEqual(result["items"], report["items"])

    def test_named_workflow_scope_rejects_unknown_value(self):
        with self.assertRaises(ValueError):
            migrate_paths.migrate_named_workflows(
                self.project, self.data, scope="somewhere"
            )

    def test_report_is_written_only_when_requested(self):
        self.make_legacy_session()
        plain = migrate_paths.run_migration(self.project, self.data, kinds={"sessions"})
        self.assertNotIn("report_path", plain)
        reported = migrate_paths.run_migration(self.project, self.data, kinds={"sessions"}, report=True)
        self.assertTrue(Path(reported["report_path"]).is_file())

    def test_invalid_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            migrate_paths.run_migration(self.project, self.data, kinds={"unknown"})


if __name__ == "__main__":
    unittest.main()
