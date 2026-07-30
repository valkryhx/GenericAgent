import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_mailbox import SubagentMailbox  # noqa: E402
from subagent_realtime_ipc import (  # noqa: E402
    SubagentRealtimeChannel,
    connect_realtime_channel,
    default_channel_address,
    write_channel_authkey,
)
from subagent_state import atomic_write_json  # noqa: E402


class SubagentRealtimeChannelTest(unittest.TestCase):
    def test_default_channel_address_is_platform_appropriate(self):
        with tempfile.TemporaryDirectory() as td:
            address = default_channel_address(Path(td), "run_000001")

            if sys.platform == "win32":
                self.assertTrue(str(address).startswith(r"\\.\pipe\\") or str(address).startswith(r"\\.\pipe"))
                self.assertIn("run_000001", str(address))
            else:
                self.assertTrue(str(address).endswith(".sock"))
                self.assertIn("run_000001", str(address))

    def test_publish_delivers_event_to_connected_client_in_realtime(self):
        with tempfile.TemporaryDirectory() as td:
            channel = SubagentRealtimeChannel(default_channel_address(Path(td), "run_pub"), authkey=b"ga-test")
            channel.start()
            self.addCleanup(channel.close)

            client = connect_realtime_channel(channel.address, authkey=b"ga-test")
            self.addCleanup(client.close)
            delivered = channel.publish({"type": "turn_started", "event_seq": 7})

            self.assertEqual(delivered, 1)
            self.assertTrue(client.poll(2))
            self.assertEqual(client.recv(), {"type": "turn_started", "event_seq": 7})
            self.assertEqual(channel.subscriber_count, 1)
            endpoint = channel.endpoint()
            self.assertEqual(endpoint["status"], "listening")
            self.assertEqual(endpoint["address"], str(channel.address))
            self.assertIn(endpoint["family"], {"AF_PIPE", "AF_UNIX"})

    def test_publish_without_subscribers_reports_zero_delivery(self):
        with tempfile.TemporaryDirectory() as td:
            channel = SubagentRealtimeChannel(default_channel_address(Path(td), "run_empty"), authkey=b"ga-test")
            channel.start()
            self.addCleanup(channel.close)

            self.assertEqual(channel.publish({"type": "noop"}), 0)
            self.assertEqual(channel.subscriber_count, 0)

    def test_publish_drops_closed_subscriber_without_raising(self):
        with tempfile.TemporaryDirectory() as td:
            channel = SubagentRealtimeChannel(default_channel_address(Path(td), "run_drop"), authkey=b"ga-test")
            channel.start()
            self.addCleanup(channel.close)

            client = connect_realtime_channel(channel.address, authkey=b"ga-test")
            channel.publish({"type": "first"})
            self.assertTrue(client.poll(2))
            client.recv()
            client.close()

            for _ in range(3):
                channel.publish({"type": "after_close"})

            # Delivery is asynchronous (see ChannelSlowSubscriberTest), so the broken pipe is
            # discovered by the sender thread rather than inside publish().
            deadline = time.time() + 5
            while channel.subscriber_count and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(channel.subscriber_count, 0)

    def test_close_stops_listening_and_reports_closed_endpoint(self):
        with tempfile.TemporaryDirectory() as td:
            channel = SubagentRealtimeChannel(default_channel_address(Path(td), "run_close"), authkey=b"ga-test")
            channel.start()
            channel.close()

            self.assertEqual(channel.endpoint()["status"], "closed")
            with self.assertRaises(Exception):
                connect_realtime_channel(channel.address, authkey=b"ga-test")


class SubagentManagerRealtimeIntegrationTest(unittest.TestCase):
    def test_manager_pushes_durable_events_to_a_live_subscriber_over_real_channel(self):
        from subagent_manager import SubagentManager

        with tempfile.TemporaryDirectory() as td:
            channels = []

            def factory(run_id, task_name):
                channel = SubagentRealtimeChannel(default_channel_address(Path(td) / "channels", run_id))
                channels.append(channel)
                return channel

            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 9911})(),
                python_executable="python-test",
                process_exists=lambda _pid: False,
                sleep=lambda _: None,
                realtime_channel_factory=factory,
            )
            handle = manager.spawn_agent("realtime_e2e", "live task", ipc_mode="pipe")
            self.addCleanup(lambda: channels[0].close())

            self.assertEqual(handle.effective_ipc_mode, "pipe")
            self.assertEqual(handle.ipc_endpoint["status"], "listening")

            client = connect_realtime_channel(handle.ipc_endpoint["address"])
            self.addCleanup(client.close)
            manager.request_foreground("realtime_e2e", reason="watch")

            self.assertTrue(client.poll(3))
            event = client.recv()
            self.assertEqual(event["type"], "foreground_requested")
            self.assertEqual(event["agent_path"], "/root/realtime_e2e")
            self.assertEqual(event["payload"]["handoff_mode"], "foreground")
            durable = manager.event_bus.read_events_since(0)
            self.assertEqual(durable[-1]["event_seq"], event["event_seq"])


class SubagentRealtimeChannelAuthTest(unittest.TestCase):
    def test_env_factory_gives_each_run_its_own_random_authkey(self):
        """A predictable address with no authkey lets any local process read the event stream.

        default_channel_address() derives the pipe/socket name from run_id, and
        SubagentRegistry hands out sequential run ids (run_000001, ...), so the address is
        guessable. Events carry task content, tool arguments and permission decisions, so
        the channel has to authenticate rather than rely on the address being secret.
        """
        from subagent_manager import SubagentManager

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"GA_SUBAGENT_REALTIME_IPC": "1"}):
                manager = SubagentManager(root_dir=td, python_executable="python-test")
            factory = manager.realtime_channel_factory
            self.assertIsNotNone(factory, "env factory should exist when GA_SUBAGENT_REALTIME_IPC=1")

            first = factory("run_000001", "alpha")
            second = factory("run_000002", "beta")

            self.assertIsInstance(first.authkey, bytes)
            self.assertGreaterEqual(len(first.authkey), 32)
            self.assertNotEqual(first.authkey, second.authkey)

    def test_unauthenticated_client_cannot_subscribe_to_an_authenticated_channel(self):
        with tempfile.TemporaryDirectory() as td:
            channel = SubagentRealtimeChannel(default_channel_address(Path(td), "run_auth"), authkey=b"x" * 32)
            channel.start()
            self.addCleanup(channel.close)

            with self.assertRaises(Exception):
                connect_realtime_channel(channel.address, authkey=None)

            self.assertEqual(channel.subscriber_count, 0)

    def test_spawn_hands_the_authkey_to_the_child_without_putting_it_in_state(self):
        """The child needs the key; read_agent must never surface it.

        ipc_endpoint is written into state.json and returned to the LLM by read_agent, so
        the key travels in a separate task-dir file that only the parent and that child
        touch.
        """
        from subagent_manager import SubagentManager
        from subagent_realtime_ipc import read_channel_authkey

        with tempfile.TemporaryDirectory() as td:
            authkey = b"k" * 32
            channels = []

            def factory(run_id, task_name):
                channel = SubagentRealtimeChannel(default_channel_address(Path(td) / "channels", run_id), authkey=authkey)
                channels.append(channel)
                return channel

            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 9912})(),
                python_executable="python-test",
                process_exists=lambda _pid: False,
                sleep=lambda _: None,
                realtime_channel_factory=factory,
            )
            handle = manager.spawn_agent("auth_e2e", "live task", ipc_mode="pipe")
            self.addCleanup(lambda: channels[0].close())

            task_dir = Path(td) / "temp" / "auth_e2e"
            self.assertEqual(read_channel_authkey(task_dir), authkey)
            self.assertNotIn(authkey.decode(), (task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertNotIn("authkey", json.dumps(handle.ipc_endpoint))


    def test_closing_an_agent_removes_its_channel_authkey(self):
        """A key left on disk after the channel is gone is a secret with no purpose.

        close_agent() tears the listener down, so the key can no longer authenticate
        anything; keeping the file only widens the window for it to be read.
        """
        from subagent_manager import SubagentManager
        from subagent_realtime_ipc import read_channel_authkey

        with tempfile.TemporaryDirectory() as td:
            channels = []

            def factory(run_id, task_name):
                channel = SubagentRealtimeChannel(
                    default_channel_address(Path(td) / "channels", run_id), authkey=b"c" * 32
                )
                channels.append(channel)
                return channel

            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 9913})(),
                python_executable="python-test",
                process_exists=lambda _pid: False,
                sleep=lambda _: None,
                realtime_channel_factory=factory,
            )
            manager.spawn_agent("authkey_cleanup", "live task", ipc_mode="pipe")
            task_dir = Path(td) / "temp" / "authkey_cleanup"
            self.assertIsNotNone(read_channel_authkey(task_dir))

            manager.close_agent("authkey_cleanup", reason="done")

            self.assertIsNone(read_channel_authkey(task_dir))


class SubagentChildSubscriptionTest(unittest.TestCase):
    """R1: the child has to actually connect, or realtime IPC changes nothing.

    connect_realtime_channel() had zero non-test callers, so GA_SUBAGENT_REALTIME_IPC=1 only
    made the parent listen and fan out to an always-empty subscriber list. Parent to child
    latency stayed at the 2s mailbox poll.
    """

    def _fake_agent(self, seen):
        class DoneQueue:
            def get(self, timeout=None):
                return {"done": "ok"}

        class FakeAgent:
            peer_hint = True
            task_dir = None
            subagent_permission_policy = None

            def put_task(self, raw, source="task"):
                seen["raw"].append(raw)
                seen["on_turn"]()
                return DoneQueue()

        return FakeAgent()

    def _prepare_task_dir(self, root, channel, authkey):
        task_dir = Path(root) / "temp" / "realtime_child"
        task_dir.mkdir(parents=True)
        (task_dir / "input.txt").write_text("initial", encoding="utf-8")
        write_channel_authkey(task_dir, authkey)
        atomic_write_json(task_dir / "state.json", {"task_name": "realtime_child", "ipc_endpoint": channel.endpoint()})
        return task_dir

    def test_worker_loop_subscribes_to_the_parent_channel(self):
        from agentmain import run_task_worker_loop

        with tempfile.TemporaryDirectory() as td:
            authkey = b"s" * 32
            channel = SubagentRealtimeChannel(default_channel_address(Path(td) / "channels", "run_child"), authkey=authkey)
            channel.start()
            self.addCleanup(channel.close)
            task_dir = self._prepare_task_dir(td, channel, authkey)
            observed = []
            seen = {"raw": [], "on_turn": lambda: observed.append(channel.subscriber_count)}

            run_task_worker_loop(
                self._fake_agent(seen), task_dir, reply_wait_iterations=1, reply_sleep_s=0, sleep_fn=lambda _: None
            )

            self.assertEqual(observed, [1], "child never subscribed to the parent realtime channel")

    def test_subscribed_child_waits_on_the_channel_instead_of_blind_sleeping(self):
        from agentmain import run_task_worker_loop

        with tempfile.TemporaryDirectory() as td:
            authkey = b"s" * 32
            channel = SubagentRealtimeChannel(default_channel_address(Path(td) / "channels", "run_signal"), authkey=authkey)
            channel.start()
            self.addCleanup(channel.close)
            task_dir = self._prepare_task_dir(td, channel, authkey)
            SubagentMailbox(task_dir / "mailbox.jsonl").enqueue(
                "do the follow-up", author="/root", recipient="/root/realtime_child", delivery_mode="trigger_turn"
            )
            slept = []
            seen = {"raw": [], "on_turn": lambda: channel.publish({"type": "message_queued", "event_seq": 1})}

            run_task_worker_loop(
                self._fake_agent(seen), task_dir, reply_wait_iterations=1, reply_sleep_s=0.05, sleep_fn=slept.append
            )

            self.assertEqual(seen["raw"], ["initial", "do the follow-up"])
            self.assertEqual(slept, [], "child blind-slept while holding a live realtime subscription")


class MailboxTriggerOverRealtimeTest(unittest.TestCase):
    """R2: parent to child latency drops to the notify round-trip, durable stays authoritative."""

    def test_send_message_persists_first_then_signals_the_subscribed_child(self):
        from subagent_manager import SubagentManager
        from subagent_realtime_ipc import open_child_subscription

        with tempfile.TemporaryDirectory() as td:
            authkey = b"t" * 32
            channels = []

            def factory(run_id, task_name):
                channel = SubagentRealtimeChannel(
                    default_channel_address(Path(td) / "channels", run_id), authkey=authkey
                )
                channels.append(channel)
                return channel

            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 9914})(),
                python_executable="python-test",
                process_exists=lambda _pid: True,
                sleep=lambda _: None,
                realtime_channel_factory=factory,
            )
            manager.spawn_agent("trigger_e2e", "live task", ipc_mode="pipe")
            self.addCleanup(lambda: channels[0].close())
            task_dir = Path(td) / "temp" / "trigger_e2e"
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            subscriber = open_child_subscription(task_dir, state)
            self.assertIsNotNone(subscriber, "child could not subscribe")
            self.addCleanup(subscriber.close)
            # A second raw connection lets the test read the payload the subscriber discards,
            # so a pass cannot come from some unrelated event waking the child.
            raw_client = connect_realtime_channel(state["ipc_endpoint"]["address"], authkey=authkey)
            self.addCleanup(raw_client.close)

            manager.followup_task("trigger_e2e", "second turn please")

            # (a) durable first
            rows = SubagentMailbox(task_dir / "mailbox.jsonl")._read_rows()
            self.assertEqual([row["content"] for row in rows], ["second turn please"])
            self.assertTrue(rows[0]["trigger_turn"])
            # (b) the child is woken well inside one poll interval, by a message_queued event
            self.assertTrue(subscriber.wait(3), "child was never signalled over the realtime channel")
            self.assertTrue(raw_client.poll(3))
            signal = raw_client.recv()
            self.assertEqual(signal["type"], "message_queued")
            self.assertEqual(signal["task_name"], "trigger_e2e")
            self.assertTrue(signal["payload"]["trigger_turn"])
            # (c) content comes from the durable mailbox, not from the signal
            self.assertNotIn("second turn please", json.dumps(signal))
            consumed = SubagentMailbox(task_dir / "mailbox.jsonl").consume_trigger_turn()
            self.assertEqual(consumed["content"], "second turn please")

    def test_signal_loss_degrades_to_polling_instead_of_losing_the_message(self):
        """A dropped notification must cost latency, never correctness."""
        from agentmain import run_task_worker_loop

        with tempfile.TemporaryDirectory() as td:
            authkey = b"t" * 32
            channel = SubagentRealtimeChannel(default_channel_address(Path(td) / "channels", "run_drop2"), authkey=authkey)
            channel.start()
            self.addCleanup(channel.close)
            task_dir = Path(td) / "temp" / "degrade_child"
            task_dir.mkdir(parents=True)
            (task_dir / "input.txt").write_text("initial", encoding="utf-8")
            write_channel_authkey(task_dir, authkey)
            atomic_write_json(task_dir / "state.json", {"task_name": "degrade_child", "ipc_endpoint": channel.endpoint()})
            # Queued but never announced on the channel.
            SubagentMailbox(task_dir / "mailbox.jsonl").enqueue(
                "silent follow-up", author="/root", recipient="/root/degrade_child", delivery_mode="trigger_turn"
            )
            seen = []

            class DoneQueue:
                def get(self, timeout=None):
                    return {"done": "ok"}

            class FakeAgent:
                peer_hint = True
                task_dir = None
                subagent_permission_policy = None

                def put_task(self, raw, source="task"):
                    seen.append(raw)
                    return DoneQueue()

            run_task_worker_loop(
                FakeAgent(), task_dir, reply_wait_iterations=2, reply_sleep_s=0.05, sleep_fn=lambda _: None
            )

            self.assertEqual(seen, ["initial", "silent follow-up"])


class ChildSubscriptionObservabilityTest(unittest.TestCase):
    """R4: a child that silently fails to subscribe is exactly how the dead channel hid for so long.

    The parent already records its own fallback via effective_ipc_mode / ipc_fallback_reason
    (subagent_ipc.normalize_ipc_metadata), but the child side had no equivalent, so
    "parent listening, child never connected" was indistinguishable from a healthy channel.
    """

    def _run_child(self, task_dir, *, iterations=1, sleep_s=0):
        from agentmain import run_task_worker_loop

        class DoneQueue:
            def get(self, timeout=None):
                return {"done": "ok"}

        class FakeAgent:
            peer_hint = True
            task_dir = None
            subagent_permission_policy = None

            def put_task(self, raw, source="task"):
                return DoneQueue()

        run_task_worker_loop(
            FakeAgent(), task_dir, reply_wait_iterations=iterations, reply_sleep_s=sleep_s, sleep_fn=lambda _: None
        )

    def _task_dir(self, root, name, endpoint, authkey=None):
        task_dir = Path(root) / "temp" / name
        task_dir.mkdir(parents=True)
        (task_dir / "input.txt").write_text("initial", encoding="utf-8")
        if authkey:
            write_channel_authkey(task_dir, authkey)
        atomic_write_json(task_dir / "state.json", {"task_name": name, "ipc_endpoint": endpoint})
        return task_dir

    def test_successful_subscription_is_recorded_in_state_and_events(self):
        with tempfile.TemporaryDirectory() as td:
            authkey = b"o" * 32
            channel = SubagentRealtimeChannel(default_channel_address(Path(td) / "channels", "run_obs_ok"), authkey=authkey)
            channel.start()
            self.addCleanup(channel.close)
            task_dir = self._task_dir(td, "obs_ok", channel.endpoint(), authkey=authkey)

            self._run_child(task_dir)

            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["child_ipc_status"], "subscribed")
            self.assertIsNone(state["child_ipc_fallback_reason"])
            events = [json.loads(line) for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            statuses = [e for e in events if e.get("type") == "child_ipc_status"]
            self.assertEqual([e["child_ipc_status"] for e in statuses], ["subscribed"])

    def test_failed_subscription_records_a_reason_and_keeps_polling(self):
        with tempfile.TemporaryDirectory() as td:
            # Address advertised as listening, but nothing is actually there.
            endpoint = {"status": "listening", "address": default_channel_address(Path(td) / "channels", "run_obs_dead")}
            task_dir = self._task_dir(td, "obs_dead", endpoint)
            SubagentMailbox(task_dir / "mailbox.jsonl").enqueue(
                "still delivered", author="/root", recipient="/root/obs_dead", delivery_mode="trigger_turn"
            )

            self._run_child(task_dir, iterations=2)

            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["child_ipc_status"], "fallback")
            self.assertIn("realtime", (state["child_ipc_fallback_reason"] or "").lower())
            consumed = SubagentMailbox(task_dir / "mailbox.jsonl")._read_rows()
            self.assertTrue(consumed[0]["consumed_at"], "durable polling must still deliver the message")

    def test_mid_flight_disconnect_is_recorded_and_falls_back_to_polling(self):
        """The parent closing its channel mid-run must be visible, not just tolerated."""
        from agentmain import run_task_worker_loop

        with tempfile.TemporaryDirectory() as td:
            authkey = b"o" * 32
            channel = SubagentRealtimeChannel(default_channel_address(Path(td) / "channels", "run_obs_cut"), authkey=authkey)
            channel.start()
            self.addCleanup(channel.close)
            task_dir = self._task_dir(td, "obs_cut", channel.endpoint(), authkey=authkey)
            SubagentMailbox(task_dir / "mailbox.jsonl").enqueue(
                "after the cut", author="/root", recipient="/root/obs_cut", delivery_mode="trigger_turn"
            )
            seen = []

            class DoneQueue:
                def get(self, timeout=None):
                    return {"done": "ok"}

            class FakeAgent:
                peer_hint = True
                task_dir = None
                subagent_permission_policy = None

                def put_task(self, raw, source="task"):
                    seen.append(raw)
                    if len(seen) == 1:
                        channel.close()
                    return DoneQueue()

            run_task_worker_loop(
                FakeAgent(), task_dir, reply_wait_iterations=3, reply_sleep_s=0.05, sleep_fn=lambda _: None
            )

            self.assertEqual(seen, ["initial", "after the cut"])
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["child_ipc_status"], "fallback")
            self.assertIn("disconnect", (state["child_ipc_fallback_reason"] or "").lower())

    def test_no_channel_advertised_is_recorded_as_file_transport_not_as_failure(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._task_dir(td, "obs_file", None)

            self._run_child(task_dir)

            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["child_ipc_status"], "file")
            self.assertIsNone(state["child_ipc_fallback_reason"])


class ChannelTransportHardeningTest(unittest.TestCase):
    """S2: the authkey proves *a* peer knows the secret, not that the endpoint is trustworthy.

    Two distinct holes remain once authentication is in place. On POSIX the channel directory
    was created with the ambient umask, so another local account could place a socket inside it
    and race the address. On Windows any process can create `\\\\.\\pipe\\ga_subagent_<run_id>`
    first — the run_id is sequential and guessable — and a child would then hand its authkey to
    an impostor server. Codex closes the same hole in
    `codex-rs/tui/src/ide_context/windows_pipe.rs:263` (`validate_pipe_server_owner`).
    """

    _AUTHKEY = b"s" * 32

    @unittest.skipIf(sys.platform == "win32", "POSIX file modes are not meaningful on Windows")
    def test_posix_channel_directory_is_private_to_its_owner(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "channels"

            default_channel_address(base, "run_perm_new")

            self.assertEqual(stat.S_IMODE(base.stat().st_mode), 0o700)

    @unittest.skipIf(sys.platform == "win32", "POSIX file modes are not meaningful on Windows")
    def test_posix_channel_directory_is_tightened_even_when_it_already_exists(self):
        """An earlier run under a loose umask must not leave the directory permanently open."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "channels"
            base.mkdir(parents=True)
            os.chmod(base, 0o777)

            default_channel_address(base, "run_perm_existing")

            self.assertEqual(stat.S_IMODE(base.stat().st_mode), 0o700)

    def _channel(self, td, run_id):
        channel = SubagentRealtimeChannel(
            default_channel_address(Path(td) / "channels", run_id), authkey=self._AUTHKEY
        )
        channel.start()
        self.addCleanup(channel.close)
        return channel

    @unittest.skipUnless(sys.platform == "win32", "named pipe owner validation is Windows-only")
    def test_pipe_owned_by_the_current_user_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            channel = self._channel(td, "run_owner_ok")

            conn = connect_realtime_channel(channel.address, authkey=self._AUTHKEY)
            self.addCleanup(conn.close)

            self.assertFalse(conn.closed)

    @unittest.skipUnless(sys.platform == "win32", "named pipe owner validation is Windows-only")
    def test_pipe_owned_by_another_user_is_refused_and_the_handle_is_closed(self):
        """A guessable address plus a first-mover impostor is the whole attack; fail closed."""
        import subagent_realtime_ipc

        with tempfile.TemporaryDirectory() as td:
            channel = self._channel(td, "run_owner_foreign")
            leaked = []
            real_client = subagent_realtime_ipc.Client

            def tracking_client(*a, **kw):
                conn = real_client(*a, **kw)
                leaked.append(conn)
                return conn

            with mock.patch.object(subagent_realtime_ipc, "Client", tracking_client), mock.patch.object(
                subagent_realtime_ipc, "_pipe_server_user_sid", return_value="S-1-5-21-9-9-9-500"
            ):
                with self.assertRaises(PermissionError) as ctx:
                    connect_realtime_channel(channel.address, authkey=self._AUTHKEY)

            self.assertIn("owner", str(ctx.exception).lower())
            self.assertEqual([c.closed for c in leaked], [True], "a refused pipe handle must not leak")

    @unittest.skipUnless(sys.platform == "win32", "named pipe owner validation is Windows-only")
    def test_owner_lookup_failure_fails_closed_rather_than_trusting_the_pipe(self):
        import subagent_realtime_ipc

        with tempfile.TemporaryDirectory() as td:
            channel = self._channel(td, "run_owner_unknown")

            with mock.patch.object(subagent_realtime_ipc, "_pipe_server_user_sid", return_value=None):
                with self.assertRaises(PermissionError):
                    connect_realtime_channel(channel.address, authkey=self._AUTHKEY)

    def test_a_refused_owner_check_degrades_the_child_to_polling_with_a_reason(self):
        """Refusing the channel must not stall the child: the durable mailbox still delivers."""
        import subagent_realtime_ipc
        from subagent_realtime_ipc import resolve_child_subscription

        with tempfile.TemporaryDirectory() as td:
            channel = self._channel(td, "run_owner_child")
            task_dir = Path(td) / "temp" / "owner_child"
            task_dir.mkdir(parents=True)
            write_channel_authkey(task_dir, self._AUTHKEY)

            with mock.patch.object(
                subagent_realtime_ipc,
                "validate_channel_owner",
                side_effect=PermissionError("realtime channel owner mismatch"),
            ):
                subscriber, status, reason = resolve_child_subscription(task_dir, {"ipc_endpoint": channel.endpoint()})

            self.assertIsNone(subscriber)
            self.assertEqual(status, "fallback")
            self.assertIn("owner mismatch", reason)

    def test_owner_validation_is_skipped_for_non_pipe_addresses(self):
        """POSIX sockets are guarded by the 0o700 directory, so the SID lookup must not run there."""
        from subagent_realtime_ipc import validate_channel_owner

        self.assertIsNone(validate_channel_owner(object(), "/tmp/ga_subagent_run_x.sock"))


class ChannelSlowSubscriberTest(unittest.TestCase):
    """B1: publish() used to send inline, so one subscriber that stopped reading froze the caller.

    Measured before the fix (docs/ga_subagent_control_plane_defects_2026-07-30.md §2): a real
    ``message_queued`` event pickles to 329 bytes and the pipe buffer swallowed exactly 24 of
    them; event 25 parked ``publish()`` — and therefore the parent's tool handler — until the
    child read. Dropping events is safe here and blocking is not: R2 makes the durable
    mailbox/event bus authoritative and the realtime event only a "go re-read" signal, so a
    dropped event costs latency while a blocked publish costs the parent its turn.
    Codex draws the same line in `app-server-client/src/lib.rs` (`AppServerEvent::Lagged`) and
    `app-server/src/transport.rs` (`try_send`, never an awaited send).
    """

    AUTHKEY = b"b1" * 16

    def _channel(self, td, run_id, **kwargs):
        channel = SubagentRealtimeChannel(
            default_channel_address(Path(td) / "channels", run_id), authkey=self.AUTHKEY, **kwargs
        )
        channel.start()
        self.addCleanup(channel.close)
        return channel

    def _subscribe(self, channel):
        client = connect_realtime_channel(channel.address, authkey=self.AUTHKEY)
        self.addCleanup(client.close)
        deadline = time.time() + 5
        while channel.subscriber_count < 1 and time.time() < deadline:
            time.sleep(0.01)
        return client

    def _drain(self, client, timeout=1.0):
        received = []
        while client.poll(timeout):
            received.append(client.recv())
            timeout = 0.2
        return received

    @staticmethod
    def _event(seq):
        return {
            "type": "message_queued",
            "event_seq": seq,
            "agent_path": "/root/slow",
            "payload": {"message_id": f"m{seq:04d}", "author": "/root", "delivery_mode": "trigger_turn"},
        }

    def test_publish_returns_promptly_when_a_subscriber_has_stopped_reading(self):
        with tempfile.TemporaryDirectory() as td:
            channel = self._channel(td, "run_b1_block", queue_size=4)
            self._subscribe(channel)

            started = time.time()
            for seq in range(200):
                channel.publish(self._event(seq))
            elapsed = time.time() - started

            self.assertLess(elapsed, 5.0, f"200 publishes took {elapsed:.2f}s against a non-reading subscriber")

    def test_a_lagging_subscriber_is_told_it_lagged_instead_of_silently_losing_its_place(self):
        """A silent gap is indistinguishable from "nothing happened"; the marker forces a re-read."""
        from subagent_realtime_ipc import CHANNEL_LAGGED

        with tempfile.TemporaryDirectory() as td:
            channel = self._channel(td, "run_b1_lag", queue_size=4)
            client = self._subscribe(channel)

            for seq in range(200):
                channel.publish(self._event(seq))

            received = self._drain(client, timeout=3.0)
            lagged = [event for event in received if event.get("type") == CHANNEL_LAGGED]
            self.assertTrue(lagged, f"no {CHANNEL_LAGGED} marker in {len(received)} received events")
            self.assertGreater(lagged[0].get("dropped", 0), 0)
            self.assertLess(len(received), 200, "a bounded queue must actually drop, not buffer everything")

    def test_the_lagged_marker_carries_no_message_body(self):
        """R2: realtime payloads are signals. A marker that carried text would leak past the mailbox."""
        from subagent_realtime_ipc import CHANNEL_LAGGED

        with tempfile.TemporaryDirectory() as td:
            channel = self._channel(td, "run_b1_body", queue_size=4)
            client = self._subscribe(channel)

            for seq in range(200):
                channel.publish(self._event(seq))

            marker = next(e for e in self._drain(client, timeout=3.0) if e.get("type") == CHANNEL_LAGGED)

            self.assertEqual(set(marker) - {"type", "dropped"}, set(), f"unexpected keys in marker: {marker}")

    def test_a_stalled_subscriber_does_not_delay_delivery_to_a_healthy_one(self):
        with tempfile.TemporaryDirectory() as td:
            channel = self._channel(td, "run_b1_isolate", queue_size=4)
            stalled = self._subscribe(channel)
            healthy = connect_realtime_channel(channel.address, authkey=self.AUTHKEY)
            self.addCleanup(healthy.close)
            deadline = time.time() + 5
            while channel.subscriber_count < 2 and time.time() < deadline:
                time.sleep(0.01)

            drained = []
            for seq in range(200):
                channel.publish(self._event(seq))
                while healthy.poll(0):
                    drained.append(healthy.recv())
            deadline = time.time() + 3
            while len(drained) < 200 and healthy.poll(0.2) and time.time() < deadline:
                drained.append(healthy.recv())

            self.assertNotIn(None, drained)
            seqs = [e["event_seq"] for e in drained if e.get("type") == "message_queued"]
            self.assertEqual(seqs, sorted(seqs), "the healthy subscriber must still see events in order")
            self.assertGreaterEqual(len(seqs), 100, f"healthy subscriber starved by the stalled one: {len(seqs)}")
            self.assertFalse(stalled.closed)

    def test_close_joins_the_sender_threads(self):
        with tempfile.TemporaryDirectory() as td:
            channel = self._channel(td, "run_b1_close", queue_size=4)
            self._subscribe(channel)
            for seq in range(200):
                channel.publish(self._event(seq))

            channel.close()

            lingering = [t for t in threading.enumerate() if "ga-subagent-realtime" in t.name and t.is_alive()]
            self.assertEqual(lingering, [], f"threads left running after close: {[t.name for t in lingering]}")


class ChannelUpstreamSignalTest(unittest.TestCase):
    """B3: the parent had no way to be *woken* by a child, only to re-read files on a timer.

    `wait_agents()` polled state.json every 0.5s, which is both slow and (measured) 20 atomic
    writes/sec. The realtime channel already exists and the duplex probe showed one Connection
    carries concurrent send+recv safely, so the child can wake the parent over the same socket
    it already subscribes on. Same shape as Codex `control.rs` `subscribe_status` →
    `watch::Receiver<AgentStatus>`: the signal carries no state, it only says "re-read".
    """

    AUTHKEY = b"b3" * 16

    def _channel(self, td, run_id):
        channel = SubagentRealtimeChannel(default_channel_address(Path(td) / "channels", run_id), authkey=self.AUTHKEY)
        channel.start()
        self.addCleanup(channel.close)
        return channel

    def _subscriber(self, channel):
        from subagent_realtime_ipc import SubagentRealtimeSubscriber

        conn = connect_realtime_channel(channel.address, authkey=self.AUTHKEY)
        subscriber = SubagentRealtimeSubscriber(conn, address=channel.address)
        self.addCleanup(subscriber.close)
        deadline = time.time() + 5
        while channel.subscriber_count < 1 and time.time() < deadline:
            time.sleep(0.01)
        return subscriber

    def test_wait_for_signal_times_out_when_nobody_signals(self):
        with tempfile.TemporaryDirectory() as td:
            channel = self._channel(td, "run_b3_idle")
            self._subscriber(channel)

            started = time.time()
            woke = channel.wait_for_signal(0.2)

            self.assertFalse(woke)
            self.assertGreaterEqual(time.time() - started, 0.15, "wait_for_signal returned before its timeout")

    def test_a_child_signal_wakes_the_parent_before_the_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            channel = self._channel(td, "run_b3_wake")
            subscriber = self._subscriber(channel)

            def signal_later():
                time.sleep(0.05)
                subscriber.signal()

            thread = threading.Thread(target=signal_later)
            thread.start()
            started = time.time()
            woke = channel.wait_for_signal(5.0)
            thread.join(timeout=2)

            self.assertTrue(woke, "parent slept through a child signal")
            self.assertLess(time.time() - started, 3.0)

    def test_wait_for_signal_returns_false_without_subscribers_instead_of_busy_looping(self):
        with tempfile.TemporaryDirectory() as td:
            channel = self._channel(td, "run_b3_empty")

            started = time.time()

            self.assertFalse(channel.wait_for_signal(0.2))
            self.assertGreaterEqual(time.time() - started, 0.15, "an empty channel must still honour the timeout")

    def test_signalling_is_best_effort_and_never_raises_after_the_channel_is_gone(self):
        with tempfile.TemporaryDirectory() as td:
            channel = self._channel(td, "run_b3_gone")
            subscriber = self._subscriber(channel)
            channel.close()

            for _ in range(3):
                self.assertIsInstance(subscriber.signal(), bool)

    def test_upstream_signals_do_not_disturb_downstream_delivery(self):
        """Both directions share one Connection; a signal must not eat an event or vice versa."""
        with tempfile.TemporaryDirectory() as td:
            channel = self._channel(td, "run_b3_duplex")
            subscriber = self._subscriber(channel)

            for seq in range(20):
                subscriber.signal()
                channel.publish({"type": "message_queued", "event_seq": seq, "agent_path": "/root/duplex"})
            woke = channel.wait_for_signal(2.0)

            self.assertTrue(woke)
            self.assertTrue(subscriber.wait(2.0), "downstream events stopped arriving once signals were in flight")
            self.assertFalse(subscriber.closed)


class ChildEventSignalsTheParentTest(unittest.TestCase):
    """Every durable event the child appends must also poke the parent; otherwise the parent's
    watch only wakes for the few events the *parent itself* publishes, and the child's
    turn_completed still costs a full poll interval."""

    AUTHKEY = b"b3c" * 10 + b"aa"

    def test_worker_loop_signals_the_parent_when_it_appends_an_event(self):
        from agentmain import run_task_worker_loop

        with tempfile.TemporaryDirectory() as td:
            channel = SubagentRealtimeChannel(
                default_channel_address(Path(td) / "channels", "run_b3_child"), authkey=self.AUTHKEY
            )
            channel.start()
            self.addCleanup(channel.close)
            task_dir = Path(td) / "temp" / "signal_child"
            task_dir.mkdir(parents=True)
            (task_dir / "input.txt").write_text("initial", encoding="utf-8")
            write_channel_authkey(task_dir, self.AUTHKEY)
            atomic_write_json(task_dir / "state.json", {"task_name": "signal_child", "ipc_endpoint": channel.endpoint()})

            class DoneQueue:
                def get(self, timeout=None):
                    return {"done": "ok"}

            class FakeAgent:
                peer_hint = True
                task_dir = None
                subagent_permission_policy = None

                def put_task(self, raw, source="task"):
                    return DoneQueue()

            woke = []

            def watch():
                while len(woke) < 1:
                    if channel.wait_for_signal(0.1):
                        woke.append(True)
                    elif len(woke) == 0 and not channel.subscriber_count:
                        break

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            run_task_worker_loop(
                FakeAgent(), task_dir, reply_wait_iterations=1, reply_sleep_s=0, sleep_fn=lambda _: None
            )
            watcher.join(timeout=5)

            self.assertEqual(woke, [True], "the child appended durable events without waking the parent")


if __name__ == "__main__":
    unittest.main()
