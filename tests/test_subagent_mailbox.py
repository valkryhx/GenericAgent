import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agentmain import run_task_worker_loop  # noqa: E402
from subagent_mailbox import SubagentMailbox  # noqa: E402


class SubagentMailboxTest(unittest.TestCase):
    def test_queue_only_does_not_trigger_turn(self):
        with tempfile.TemporaryDirectory() as td:
            mailbox = SubagentMailbox(Path(td) / "mailbox.jsonl")

            row = mailbox.enqueue("note", author="/root", recipient="/root/demo", delivery_mode="queue_only")

            self.assertFalse(row["trigger_turn"])
            self.assertEqual(row["delivery_mode"], "queue_only")
            self.assertIsNone(mailbox.consume_trigger_turn())

    def test_trigger_turn_consumes_prior_queue_only_messages_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            mailbox = SubagentMailbox(Path(td) / "mailbox.jsonl")
            mailbox.enqueue("first note", author="/root", recipient="/root/demo", delivery_mode="queue_only")
            mailbox.enqueue("run this", author="/root", recipient="/root/demo", delivery_mode="trigger_turn")

            result = mailbox.consume_trigger_turn()

            self.assertEqual(result["content"], "first note\n\nrun this")
            self.assertEqual([row["content"] for row in result["messages"]], ["first note", "run this"])
            rows = [json.loads(line) for line in (Path(td) / "mailbox.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(row["consumed_at"] for row in rows))
            self.assertTrue(all(row["acknowledged_at"] for row in rows))

    def test_malformed_rows_are_skipped_and_message_id_is_deduped(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mailbox.jsonl"
            path.write_text("not-json\n", encoding="utf-8")
            mailbox = SubagentMailbox(path)
            mailbox.enqueue("first", message_id="msg_fixed", author="/root", recipient="/root/demo", delivery_mode="trigger_turn")
            mailbox.enqueue("duplicate", message_id="msg_fixed", author="/root", recipient="/root/demo", delivery_mode="trigger_turn")

            result = mailbox.consume_trigger_turn()

            self.assertEqual(result["content"], "first")
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("{")]
            self.assertEqual(len(rows), 1)


    def test_concurrent_enqueue_keeps_every_message(self):
        """Durable mailbox is only authoritative if concurrent writers never overwrite each other.

        enqueue() is a read-modify-write over the whole file, so without a cross-process
        lock each writer persists its own stale snapshot and the last writer wins. Separate
        SubagentMailbox instances stand in for separate processes.
        """
        writers = 6
        rounds = 4
        for round_no in range(rounds):
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "mailbox.jsonl"
                start = threading.Barrier(writers)
                errors = []

                def worker(index):
                    try:
                        start.wait(timeout=5)
                        SubagentMailbox(path).enqueue(
                            f"note-{index}",
                            author="/root",
                            recipient="/root/demo",
                            delivery_mode="queue_only",
                            message_id=f"msg_fixed_{index}",
                        )
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=worker, args=(index,)) for index in range(writers)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)

                self.assertEqual(errors, [])
                rows = SubagentMailbox(path)._read_rows()
                self.assertEqual(
                    len(rows),
                    writers,
                    f"round {round_no}: expected {writers} persisted messages, got {len(rows)}",
                )
                self.assertEqual(
                    sorted(row["message_id"] for row in rows),
                    sorted(f"msg_fixed_{index}" for index in range(writers)),
                )

    def test_enqueue_is_not_lost_when_consume_runs_concurrently(self):
        """Parent send_message and child consume are concurrent on the real path.

        consume_trigger_turn() also rewrites the whole file, so it can clobber a message
        the parent just appended. A message must end up either persisted or consumed.
        """
        rounds = 20
        for round_no in range(rounds):
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "mailbox.jsonl"
                SubagentMailbox(path).enqueue(
                    "already queued",
                    author="/root",
                    recipient="/root/demo",
                    delivery_mode="trigger_turn",
                    message_id="msg_old",
                )
                start = threading.Barrier(2)
                consumed = {}
                errors = []

                def child():
                    try:
                        start.wait(timeout=5)
                        consumed["result"] = SubagentMailbox(path).consume_trigger_turn()
                    except Exception as exc:
                        errors.append(exc)

                def parent():
                    try:
                        start.wait(timeout=5)
                        SubagentMailbox(path).enqueue(
                            "sent while consuming",
                            author="/root",
                            recipient="/root/demo",
                            delivery_mode="trigger_turn",
                            message_id="msg_new",
                        )
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=child), threading.Thread(target=parent)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)

                self.assertEqual(errors, [])
                persisted = {row["message_id"] for row in SubagentMailbox(path)._read_rows()}
                result = consumed.get("result") or {}
                consumed_ids = {row.get("message_id") for row in (result.get("messages") or [])}
                self.assertTrue(
                    "msg_new" in persisted or "msg_new" in consumed_ids,
                    f"round {round_no}: msg_new was neither persisted nor consumed, i.e. lost",
                )

    def test_only_one_mailbox_consume_implementation_exists(self):
        """Two consume implementations had already drifted apart in both safety and semantics.

        subagent_state.consume_mailbox_trigger() wrote atomically but consumed only the
        first trigger row, while the actually-called SubagentMailbox.consume_trigger_turn()
        truncate-rewrote and consumed the trigger plus every preceding queue_only message.
        Keeping the unused fork around invites a future caller to pick the wrong one.
        """
        import subagent_state

        self.assertFalse(
            hasattr(subagent_state, "consume_mailbox_trigger"),
            "subagent_state still exposes a second mailbox consumer; SubagentMailbox.consume_trigger_turn is authoritative",
        )
        self.assertNotIn(
            "consume_mailbox_trigger",
            (REPO_ROOT / "agentmain.py").read_text(encoding="utf-8"),
            "agentmain still references the removed fork",
        )

    def test_auto_message_id_never_reuses_an_existing_id(self):
        """A count-derived auto id can equal an id the mailbox already holds.

        enqueue() derives msg_%06d from the current row count, so a mailbox that already
        contains an id inside that namespace — caller-supplied, or written by another
        process — makes the next auto id collide. The dedup branch then returns the OLD
        row and silently discards the new message, so a dropped send looks like a
        successful idempotent one.
        """
        with tempfile.TemporaryDirectory() as td:
            mailbox = SubagentMailbox(Path(td) / "mailbox.jsonl")
            mailbox.enqueue(
                "explicit",
                author="/root",
                recipient="/root/demo",
                delivery_mode="queue_only",
                message_id="msg_000002",
            )

            row = mailbox.enqueue("auto", author="/root", recipient="/root/demo", delivery_mode="queue_only")

            self.assertEqual(row["content"], "auto")
            self.assertEqual([r["content"] for r in mailbox._read_rows()], ["explicit", "auto"])

    def test_unlocked_reader_never_sees_a_truncated_mailbox(self):
        """Readers outside the lock must never observe a partial file.

        _write_rows() truncates then re-writes, so any reader that is not holding the
        lock — the CLI inspecting mailbox.jsonl, a monitor tailing it, or a future
        lock-free fast path — can land inside that window and see fewer rows than were
        ever committed. An atomic replace closes the window entirely.
        """
        seeded = 40
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mailbox.jsonl"
            mailbox = SubagentMailbox(path)
            for index in range(seeded):
                mailbox.enqueue(
                    f"note-{index}",
                    author="/root",
                    recipient="/root/demo",
                    delivery_mode="queue_only",
                    message_id=f"msg_seed_{index:04d}",
                )

            stop = threading.Event()
            short_reads = []
            errors = []

            def rewriter():
                try:
                    while not stop.is_set():
                        # Re-enqueueing a known message_id is a no-op append-wise but still
                        # exercises the full read-modify-write, same as the real send path.
                        mailbox.enqueue(
                            "note-0",
                            author="/root",
                            recipient="/root/demo",
                            delivery_mode="queue_only",
                            message_id="msg_seed_0000",
                        )
                        mailbox._write_rows(mailbox._read_rows())
                except Exception as exc:
                    errors.append(exc)

            def reader():
                try:
                    while not stop.is_set():
                        try:
                            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                        except OSError:
                            continue
                        if len(lines) < seeded:
                            short_reads.append(len(lines))
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=rewriter), threading.Thread(target=reader)]
            for thread in threads:
                thread.start()
            time.sleep(1.0)
            stop.set()
            for thread in threads:
                thread.join(timeout=5)

            self.assertEqual(errors, [])
            self.assertEqual(
                short_reads[:5],
                [],
                f"{len(short_reads)} truncated reads observed, e.g. row counts {short_reads[:5]} < {seeded}",
            )

    def test_worker_uses_mailbox_main_path_without_reply_txt(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo"
            task_dir.mkdir(parents=True)
            (task_dir / "input.txt").write_text("initial", encoding="utf-8")
            mailbox = SubagentMailbox(task_dir / "mailbox.jsonl")
            mailbox.enqueue("queued", author="/root", recipient="/root/demo", delivery_mode="queue_only")
            mailbox.enqueue("trigger", author="/root", recipient="/root/demo", delivery_mode="trigger_turn")
            seen = {"count": 0, "raw": []}

            class DoneQueue:
                def get(self, timeout=None):
                    return {"done": "ok"}

            class FakeAgent:
                peer_hint = True
                task_dir = None
                subagent_permission_policy = None

                def put_task(self, raw, source="task"):
                    seen["count"] += 1
                    seen["raw"].append(raw)
                    return DoneQueue()

            run_task_worker_loop(FakeAgent(), task_dir, reply_wait_iterations=1, reply_sleep_s=0, sleep_fn=lambda _: None)

            self.assertEqual(seen["raw"], ["initial", "queued\n\ntrigger"])
            self.assertFalse((task_dir / "reply.txt").exists())
            rows = [json.loads(line) for line in (task_dir / "mailbox.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(row["consumed_at"] for row in rows))


if __name__ == "__main__":
    unittest.main()
