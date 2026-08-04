import sys
import tempfile
import threading
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_submissions import SubagentSubmissionLog  # noqa: E402


class SubmissionLogTest(unittest.TestCase):
    """B2: a control-plane op needs an identity, or a replayed call runs twice.

    Measured before this existed: calling followup_task twice with the same logical submission
    queued two trigger_turn rows, i.e. the child ran the task twice. Codex gives every op a
    `Submission { id, op, trace }` (protocol.rs) for exactly this reason.
    """

    def test_a_recorded_submission_is_found_again(self):
        with tempfile.TemporaryDirectory() as td:
            log = SubagentSubmissionLog(td)

            log.record("sub_1", op="followup_task", target="/root/w", result={"message_id": "msg_000001"})

            found = log.find("sub_1")
            self.assertEqual(found["op"], "followup_task")
            self.assertEqual(found["target"], "/root/w")
            self.assertEqual(found["result"], {"message_id": "msg_000001"})
            self.assertTrue(found["created_at"])

    def test_an_unknown_submission_is_none_rather_than_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(SubagentSubmissionLog(td).find("never_seen"))

    def test_an_empty_submission_id_is_never_recorded(self):
        """No id means "don't dedup me"; storing it under "" would make unrelated calls collide."""
        with tempfile.TemporaryDirectory() as td:
            log = SubagentSubmissionLog(td)

            for blank in (None, "", "   "):
                self.assertIsNone(log.record(blank, op="send_message", target="/root/w", result={}))
                self.assertIsNone(log.find(blank))

    def test_recording_the_same_id_twice_keeps_the_first_result(self):
        """The first execution is the one that really happened; a replay must not overwrite it."""
        with tempfile.TemporaryDirectory() as td:
            log = SubagentSubmissionLog(td)

            log.record("sub_2", op="spawn_agent", target="/root/w", result={"run_id": "run_000001"})
            log.record("sub_2", op="spawn_agent", target="/root/w", result={"run_id": "run_000002"})

            self.assertEqual(log.find("sub_2", op="spawn_agent", target="/root/w")["result"], {"run_id": "run_000001"})

    def test_the_same_id_on_a_different_target_is_a_different_submission(self):
        """This file outlives the process, so a stale row must not answer for another agent.

        Measured on the real guard E2E: a row written 2026-07-30 for one agent made a 2026-08-03
        call for a different agent look already-done, and the caller got the old agent's result
        back. The id says "same call"; the op and target say which call.
        """
        with tempfile.TemporaryDirectory() as td:
            log = SubagentSubmissionLog(td)

            log.record("retry", op="spawn_agent", target="/root/alpha", result={"run_id": "run_1"})
            log.record("retry", op="spawn_agent", target="/root/beta", result={"run_id": "run_2"})

            self.assertEqual(log.find("retry", op="spawn_agent", target="/root/alpha")["result"], {"run_id": "run_1"})
            self.assertEqual(log.find("retry", op="spawn_agent", target="/root/beta")["result"], {"run_id": "run_2"})
            self.assertIsNone(log.find("retry", op="spawn_agent", target="/root/gamma"))

    def test_the_same_id_and_target_on_a_different_op_is_a_different_submission(self):
        with tempfile.TemporaryDirectory() as td:
            log = SubagentSubmissionLog(td)

            log.record("one_id", op="spawn_agent", target="/root/w", result={"phase": "spawned"})
            log.record("one_id", op="close_agent", target="/root/w", result={"phase": "closed"})

            self.assertEqual(log.find("one_id", op="close_agent", target="/root/w")["result"], {"phase": "closed"})
            self.assertEqual(log.find("one_id", op="spawn_agent", target="/root/w")["result"], {"phase": "spawned"})

    def test_a_result_that_cannot_be_serialized_is_still_recorded_as_a_replay_marker(self):
        """Dedup matters more than the payload: without a row, the replay re-executes."""
        with tempfile.TemporaryDirectory() as td:
            log = SubagentSubmissionLog(td)

            log.record("sub_3", op="close_agent", target="/root/w", result={"handle": object()})

            found = log.find("sub_3")
            self.assertIsNotNone(found, "an unserializable result must not silently skip the record")
            self.assertEqual(found["op"], "close_agent")

    def test_concurrent_writers_never_lose_a_submission(self):
        """Same defect class as M1/M5: every child process is another writer on this one file."""
        WRITERS, ROUNDS = 4, 12
        with tempfile.TemporaryDirectory() as td:
            barrier = threading.Barrier(WRITERS)
            failures = []

            def worker(index):
                log = SubagentSubmissionLog(td)
                barrier.wait()
                try:
                    for round_no in range(ROUNDS):
                        log.record(f"sub_{index}_{round_no}", op="send_message", target="/root/w", result={})
                except Exception as exc:  # pragma: no cover - only on a real defect
                    failures.append(f"{type(exc).__name__}: {exc}")

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(WRITERS)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            self.assertEqual(failures, [])
            log = SubagentSubmissionLog(td)
            missing = [
                f"sub_{i}_{r}"
                for i in range(WRITERS)
                for r in range(ROUNDS)
                if log.find(f"sub_{i}_{r}") is None
            ]
            self.assertEqual(missing, [], f"{len(missing)}/{WRITERS * ROUNDS} submissions lost")


if __name__ == "__main__":
    unittest.main()
