import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MEMORY_DIR = REPO_ROOT / "memory"
if str(MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_DIR))


from codex_session_distill import (  # noqa: E402
    DistillState,
    codex_lesson_update,
    discover_codex_session_roots,
    extract_session_packet,
    learn_from_packets,
    promote_candidates,
    prepare_sessions,
    render_sop,
)


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _function_call(name, arguments=None):
    return {
        "timestamp": "2026-05-09T12:00:00Z",
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": name,
            "arguments": json.dumps(arguments or {}, ensure_ascii=False),
            "call_id": f"call_{name}",
        },
    }


def _function_output(call_id, output):
    return {
        "timestamp": "2026-05-09T12:00:01Z",
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        },
    }


class CodexSessionDistillTest(unittest.TestCase):
    def test_discover_codex_session_roots_checks_home_appdata_and_workspace_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            appdata = tmp_path / "appdata"
            workspace = tmp_path / "workspace"
            first = home / ".codex" / "sessions"
            second = appdata / "Codex" / "sessions"
            third = workspace / ".codex" / "sessions"
            for path in (first, second, third):
                path.mkdir(parents=True)

            roots = discover_codex_session_roots(home=home, appdata=appdata, cwd=workspace, env={})

            self.assertEqual(roots, [first.resolve(), second.resolve(), third.resolve()])

    def test_extract_session_packet_redacts_sensitive_text_and_finds_coding_lessons(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "rollout.jsonl"
            _write_jsonl(
                session,
                [
                    {
                        "timestamp": "2026-05-09T12:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "cwd": r"C:\Users\Administrator\secret_project",
                            "id": "s1",
                        },
                    },
                    {
                        "timestamp": "2026-05-09T12:00:00Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "fix bug with sk-test-secret-1234567890"},
                    },
                    _function_call("shell_command", {"command": "rg -n \"broken\" .", "workdir": r"C:\Users\Administrator\secret_project"}),
                    _function_output("call_shell_command", "Exit code: 0\nbroken found"),
                    _function_call("apply_patch", {"patch": "*** Begin Patch\n*** Update File: app.py\n*** End Patch"}),
                    _function_call("shell_command", {"command": "python -m unittest discover -s tests"}),
                    _function_output("call_shell_command", "Exit code: 0\nOK"),
                    {
                        "timestamp": "2026-05-09T12:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "done"}],
                        },
                    },
                ],
            )

            packet = extract_session_packet(session)
            markdown = packet.to_markdown()

        lesson_ids = {lesson["id"] for lesson in packet.lessons}
        self.assertIn("repo_probe_before_edit", lesson_ids)
        self.assertIn("verify_changes_before_done", lesson_ids)
        self.assertIn("prefer_fast_text_search", lesson_ids)
        self.assertGreaterEqual(packet.quality, 0.6)
        self.assertNotIn("sk-test-secret", markdown)
        self.assertNotIn(r"C:\Users\Administrator", markdown)
        self.assertIn("<REDACTED_SECRET>", markdown)
        self.assertIn("<PATH>", markdown)

    def test_prepare_sessions_records_progress_and_skips_processed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sessions = tmp_path / "sessions"
            sessions.mkdir()
            state = DistillState(tmp_path / "state")
            session = sessions / "rollout.jsonl"
            _write_jsonl(
                session,
                [
                    {"timestamp": "2026-05-09T12:00:00Z", "type": "session_meta", "payload": {"cwd": "repo"}},
                    _function_call("shell_command", {"command": "rg -n target ."}),
                    _function_call("apply_patch", {"patch": "*** Begin Patch\n*** End Patch"}),
                    _function_call("shell_command", {"command": "python -m unittest discover -s tests"}),
                    _function_output("call_shell_command", "Exit code: 0\nOK"),
                ],
            )

            first = prepare_sessions([sessions], state, limit=10, min_quality=0.1)
            second = prepare_sessions([sessions], state, limit=10, min_quality=0.1)

            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            progress = state.load_progress()
            only_record = next(iter(progress["sessions"].values()))
            self.assertEqual(only_record["status"], "prepared")
            self.assertEqual(only_record["learn_count"], 0)

    def test_prepare_sessions_processes_files_in_stable_path_order_not_random_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sessions = tmp_path / "sessions"
            (sessions / "2026" / "05").mkdir(parents=True)
            (sessions / "2026" / "04").mkdir(parents=True)
            for rel in ("2026/05/b.jsonl", "2026/04/a.jsonl"):
                _write_jsonl(
                    sessions / rel,
                    [
                        {"timestamp": "2026-05-09T12:00:00Z", "type": "session_meta", "payload": {"cwd": "repo"}},
                        _function_call("shell_command", {"command": "rg -n target ."}),
                        _function_call("apply_patch", {"patch": "*** Begin Patch\n*** End Patch"}),
                        _function_call("shell_command", {"command": "python -m unittest discover -s tests"}),
                        _function_output("call_shell_command", "Exit code: 0\nOK"),
                    ],
                )

            packets = prepare_sessions([sessions], DistillState(tmp_path / "state"), limit=1, min_quality=0.1)

            self.assertEqual(len(packets), 1)
            self.assertTrue(packets[0].source.endswith("2026\\04\\a.jsonl") or packets[0].source.endswith("2026/04/a.jsonl"))

    def test_learn_from_packets_merges_repeated_lessons_and_render_sop(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state = DistillState(tmp_path / "state")
            queue = state.queue_dir
            queue.mkdir(parents=True, exist_ok=True)

            packet_body = {
                "session_hash": "abc123",
                "source": "rollout.jsonl",
                "quality": 0.9,
                "focus": "workflow",
                "lessons": [
                    {
                        "id": "repo_probe_before_edit",
                        "category": "workflow",
                        "title": "改代码前先探测仓库事实",
                        "guidance": "编码任务先读项目规范、状态和相关代码，再做最小补丁。",
                        "signals": ["fast_search", "patch"],
                    }
                ],
            }
            for index in range(2):
                current = dict(packet_body)
                current["session_hash"] = f"abc12{index}"
                with open(queue / f"packet-{index}.json", "w", encoding="utf-8") as f:
                    json.dump(current, f, ensure_ascii=False)

            learned = learn_from_packets(state, limit=10)
            sop = render_sop(state, tmp_path / "codex_coding_sop.md")
            lessons = [json.loads(line) for line in state.lessons_path.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(learned, 2)
            self.assertEqual(len(lessons), 1)
            self.assertEqual(lessons[0]["evidence_count"], 2)
            self.assertIn("改代码前先探测仓库事实", sop)
            self.assertIn("证据: 2", sop)

    def test_codex_lesson_update_validates_redacts_and_promotes_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = DistillState(Path(tmp) / "state")

            result = codex_lesson_update(
                state,
                title="保护用户未提交改动",
                guidance="改代码前检查 git status，遇到无关改动只绕开，不重置或覆盖。",
                category="git",
                evidence=["git_status", "patch"],
                source_hash="abc123",
                confidence=0.9,
            )

            self.assertEqual(result["status"], "candidate_recorded")
            self.assertEqual(result["candidate"]["id"], "git_protect_user_worktree")
            promoted = promote_candidates(state, min_evidence=1, min_confidence=0.85)
            self.assertEqual(promoted, 1)
            sop = render_sop(state, Path(tmp) / "sop.md")
            self.assertIn("保护用户未提交改动", sop)

            bad = codex_lesson_update(
                state,
                title="泄露路径",
                guidance=r"读取 C:\Users\Administrator\secret\.env 里的 token=abc",
                category="security",
                evidence=["secret"],
                source_hash="abc124",
                confidence=0.99,
            )

            self.assertEqual(bad["status"], "rejected")
            self.assertIn("sensitive", bad["reason"])


if __name__ == "__main__":
    unittest.main()
