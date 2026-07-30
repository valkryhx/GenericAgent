from __future__ import annotations

import collections
import os
import secrets
import threading
from multiprocessing.connection import Client, Listener, address_type
from pathlib import Path


SUBSCRIBED_ACK = "channel_subscribed"
CHANNEL_LAGGED = "channel_lagged"
DEFAULT_QUEUE_SIZE = 64
AUTHKEY_FILENAME = "ipc_authkey"
AUTHKEY_BYTES = 32


def new_channel_authkey():
    """Per-run shared secret for the realtime channel.

    The channel address is derived from the sequential run_id, so it is guessable by any
    local process; the events it carries include task text, tool arguments and permission
    decisions. Authentication, not address secrecy, is what keeps the stream private.
    """
    return secrets.token_bytes(AUTHKEY_BYTES)


def write_channel_authkey(task_dir, authkey):
    """Hand the key to one child via its own task dir, never via state.json.

    state.json is returned to the LLM by read_agent, so the key travels out-of-band in a
    sidecar file that only the parent and that child touch.
    """
    if not authkey:
        return None
    path = Path(task_dir) / AUTHKEY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(authkey))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def read_channel_authkey(task_dir):
    try:
        data = (Path(task_dir) / AUTHKEY_FILENAME).read_bytes()
    except OSError:
        return None
    return data or None


def remove_channel_authkey(task_dir):
    """Drop the key once its channel is closed; it can no longer authenticate anything."""
    try:
        (Path(task_dir) / AUTHKEY_FILENAME).unlink()
        return True
    except OSError:
        return False


def default_channel_address(base_dir, run_id):
    """Return a platform-appropriate realtime channel address for a subagent run."""
    safe_run_id = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in str(run_id or "run"))
    if _is_windows():
        return rf"\\.\pipe\ga_subagent_{safe_run_id}"
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    # The authkey proves a peer knows the secret; it says nothing about who may reach the
    # address. Under the ambient umask another local account could drop its own socket into
    # this directory and race the address, so tighten it on every call rather than only at
    # creation — an earlier run under a loose umask must not leave it permanently open.
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return str(base / f"ga_subagent_{safe_run_id}.sock")


def _is_pipe_address(address):
    try:
        return address_type(str(address)) == "AF_PIPE"
    except Exception:
        return False


def _sid_to_string(sid_ptr):
    import ctypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    text = ctypes.c_wchar_p()
    if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(text)):
        return None
    return text.value


def _process_user_sid(process_handle):
    """SID string of the user owning ``process_handle``, or None if it cannot be read."""
    import ctypes
    import ctypes.wintypes as wt

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TOKEN_QUERY = 0x0008
    TokenUser = 1

    token = wt.HANDLE()
    if not advapi32.OpenProcessToken(wt.HANDLE(process_handle), wt.DWORD(TOKEN_QUERY), ctypes.byref(token)):
        return None
    try:
        size = wt.DWORD(0)
        advapi32.GetTokenInformation(token, ctypes.c_int(TokenUser), None, wt.DWORD(0), ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, ctypes.c_int(TokenUser), buf, size, ctypes.byref(size)):
            return None

        class _SidAndAttributes(ctypes.Structure):
            # TOKEN_USER is just a SID_AND_ATTRIBUTES; only its Sid pointer is needed.
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]

        user = ctypes.cast(buf, ctypes.POINTER(_SidAndAttributes)).contents
        return _sid_to_string(ctypes.c_void_p(user.Sid))
    finally:
        kernel32.CloseHandle(token)


def _pipe_server_user_sid(conn):
    """SID string of the user running the named-pipe server behind ``conn``.

    Mirrors Codex `validate_pipe_server_owner` (codex-rs/tui/src/ide_context/windows_pipe.rs):
    ask the pipe for its server pid, then read that process token's user.
    """
    import ctypes
    import ctypes.wintypes as wt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wt.HANDLE
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    try:
        handle = wt.HANDLE(conn.fileno())
    except Exception:
        return None
    pid = wt.ULONG(0)
    if not kernel32.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
        return None
    process = kernel32.OpenProcess(wt.DWORD(PROCESS_QUERY_LIMITED_INFORMATION), wt.BOOL(False), wt.DWORD(pid.value))
    if not process:
        return None
    try:
        return _process_user_sid(process)
    finally:
        kernel32.CloseHandle(process)


def _current_user_sid():
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    return _process_user_sid(kernel32.GetCurrentProcess())


def validate_channel_owner(conn, address):
    """Refuse a realtime channel whose server is not run by the current user.

    Windows named pipes live in a global namespace and the address is derived from a
    sequential run_id, so any local process can create `\\\\.\\pipe\\ga_subagent_<run_id>`
    first and collect the authkey a child sends it. Ownership is what the authkey cannot
    prove. POSIX sockets need no check here: the 0o700 channel directory already bounds
    who can create or reach the address.

    Fails closed — an unreadable owner is treated as an untrusted one, because the only
    reason to be unable to read it is that the server is not ours to inspect.
    """
    if not _is_windows() or not _is_pipe_address(address):
        return None
    server_sid = _pipe_server_user_sid(conn)
    current_sid = _current_user_sid()
    if server_sid and current_sid and server_sid == current_sid:
        return None
    raise PermissionError(
        f"realtime channel owner mismatch: {address} is served by {server_sid or 'an unknown user'}, "
        f"expected {current_sid or 'the current user'}"
    )


def connect_realtime_channel(address, *, authkey=None, ack_timeout=5.0):
    """Connect to a realtime channel and consume the server subscription ack."""
    conn = Client(str(address), authkey=authkey)
    try:
        validate_channel_owner(conn, address)
    except Exception:
        # Never leave a handle open on a channel we just refused to trust.
        try:
            conn.close()
        except Exception:
            pass
        raise
    if conn.poll(ack_timeout):
        try:
            conn.recv()
        except EOFError:
            pass
    return conn


class SubagentRealtimeSubscriber:
    """Child-side end of the realtime channel.

    Carries no authority: an incoming event only means "something changed, go re-read the
    durable source", same as Codex's `watch::Sender<()>` empty-payload signal. That keeps
    mailbox.jsonl / events.jsonl authoritative and makes a dropped notification a latency
    problem rather than a correctness one.
    """

    def __init__(self, conn, *, address=None):
        self.conn = conn
        self.address = address
        self.closed = False
        self.received = 0

    def wait(self, timeout):
        """Block up to timeout for a change signal. Returns True if one arrived."""
        if self.closed or self.conn is None:
            return False
        try:
            if not self.conn.poll(timeout):
                return False
            while True:
                self.conn.recv()
                self.received += 1
                if not self.conn.poll(0):
                    return True
        except (EOFError, OSError):
            self.close()
            return self.received > 0
        except Exception:
            self.close()
            return False

    def close(self):
        self.closed = True
        conn, self.conn = self.conn, None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass


CHILD_IPC_SUBSCRIBED = "subscribed"
CHILD_IPC_FALLBACK = "fallback"
CHILD_IPC_FILE = "file"
_FILE_FALLBACK = "durable file event bus stays the transport"


def resolve_child_subscription(task_dir, state, *, ack_timeout=5.0):
    """Subscribe the child to its parent's channel and describe the outcome.

    Returns ``(subscriber, status, fallback_reason)``. A failure is never raised: realtime
    is an accelerator, and a child that cannot subscribe must still make progress on the
    durable mailbox. But it must not fail *silently* either — the parent already records
    its own fallback via ``effective_ipc_mode`` / ``ipc_fallback_reason``, and the child
    having no equivalent is why the never-connected channel went unnoticed.
    """
    endpoint = (state or {}).get("ipc_endpoint") or {}
    if not isinstance(endpoint, dict) or not endpoint:
        return None, CHILD_IPC_FILE, None
    if endpoint.get("status") != "listening":
        return None, CHILD_IPC_FALLBACK, f"realtime channel is not listening; {_FILE_FALLBACK}"
    address = endpoint.get("address")
    if not address:
        return None, CHILD_IPC_FALLBACK, f"realtime channel advertised no address; {_FILE_FALLBACK}"
    try:
        conn = connect_realtime_channel(address, authkey=read_channel_authkey(task_dir), ack_timeout=ack_timeout)
    except Exception as e:
        return None, CHILD_IPC_FALLBACK, f"realtime channel subscribe failed ({type(e).__name__}: {e}); {_FILE_FALLBACK}"
    return SubagentRealtimeSubscriber(conn, address=address), CHILD_IPC_SUBSCRIBED, None


def open_child_subscription(task_dir, state, *, ack_timeout=5.0):
    """Subscribe the child to its parent's channel, or return None to stay on polling."""
    subscriber, _status, _reason = resolve_child_subscription(task_dir, state, ack_timeout=ack_timeout)
    return subscriber


def _force_shutdown(conn):
    """Unblock a thread parked in ``conn.send()`` so close() can join it.

    Closing the handle is enough on Windows — the pending overlapped write is cancelled — but a
    POSIX socket send does not return just because the fd was closed, so the connection is shut
    down through a dup'd descriptor first (shutdown acts on the socket, not on one fd).
    """
    if os.name != "nt":
        try:
            import socket

            fd = os.dup(conn.fileno())
        except Exception:
            fd = None
        if fd is not None:
            try:
                sock = socket.socket(fileno=fd)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
            else:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                finally:
                    sock.close()
    try:
        conn.close()
    except Exception:
        pass


class _SubscriberSink:
    """One subscriber: a bounded queue plus the thread that owns its connection.

    ``publish()`` only appends here, so a subscriber that stops reading fills its own queue and
    is told it lagged instead of parking the publisher. Before this, publish() sent inline and a
    busy child froze the parent's tool handler after 24 unread events
    (docs/ga_subagent_control_plane_defects_2026-07-30.md §2). Dropping is the right failure mode
    because R2 keeps mailbox.jsonl / events.jsonl authoritative — a lost signal costs latency,
    a blocked publish costs the parent its turn.
    """

    def __init__(self, conn, *, queue_size, name="ga-subagent-realtime-send"):
        self.conn = conn
        self.queue_size = max(1, int(queue_size))
        self.alive = True
        self._queue = collections.deque()
        self._dropped = 0
        self._lagged_pending = False
        self._stopping = False
        self._cond = threading.Condition()
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def offer(self, event):
        """Queue one event. Never blocks, never raises; False means the peer is gone."""
        with self._cond:
            if self._stopping or not self.alive:
                return False
            while len(self._queue) >= self.queue_size:
                self._queue.popleft()
                self._dropped += 1
                self._lagged_pending = True
            self._queue.append(event)
            self._cond.notify()
            return True

    def _next_event(self):
        with self._cond:
            while not self._stopping and not self._queue and not self._lagged_pending:
                self._cond.wait()
            if self._stopping:
                return None
            if self._lagged_pending:
                # Marker jumps the queue on purpose: it says "you missed some, re-read the
                # durable source", and it carries no message body for the same reason the
                # normal events don't — the mailbox is the only place text comes from.
                dropped, self._dropped, self._lagged_pending = self._dropped, 0, False
                return {"type": CHANNEL_LAGGED, "dropped": dropped}
            return self._queue.popleft()

    def _run(self):
        while True:
            event = self._next_event()
            if event is None:
                break
            try:
                if getattr(self.conn, "closed", False):
                    raise OSError("connection closed")
                self.conn.send(event)
            except Exception:
                break
        with self._cond:
            self.alive = False
            self._queue.clear()
        # The thread owns the connection, so it also owns closing it: a send error is how a
        # subscriber usually disappears, and subscriber_count prunes the dead sink without
        # ever touching the handle.
        try:
            self.conn.close()
        except Exception:
            pass

    def close(self):
        with self._cond:
            self._stopping = True
            self.alive = False
            self._queue.clear()
            self._lagged_pending = False
            self._cond.notify_all()
        _force_shutdown(self.conn)
        thread = self.thread
        if thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)


class SubagentRealtimeChannel:
    """Resident realtime fan-out channel over multiprocessing connections.

    The durable file event bus stays the source of truth; this channel only pushes
    already-persisted events to attached subscribers with lower latency.
    """

    def __init__(self, address, *, authkey=None, backlog=8, queue_size=DEFAULT_QUEUE_SIZE):
        self.address = str(address)
        self.authkey = authkey
        self.backlog = backlog
        self.queue_size = queue_size
        self._listener = None
        self._thread = None
        self._subscribers = []
        self._lock = threading.Lock()
        self._closed = False

    def start(self):
        if self._listener is not None:
            return self
        self._listener = Listener(self.address, authkey=self.authkey, backlog=self.backlog)
        self.address = str(self._listener.address)
        self._closed = False
        self._thread = threading.Thread(target=self._accept_loop, name="ga-subagent-realtime-accept", daemon=True)
        self._thread.start()
        return self

    def publish(self, event):
        """Hand the event to every subscriber's queue and return how many accepted it.

        Returns the number *queued*, not the number written to a socket: the write happens on
        each subscriber's own thread, so this call cannot be held up by a subscriber that has
        stopped reading.
        """
        with self._lock:
            subscribers = list(self._subscribers)
        delivered = 0
        broken = []
        for sink in subscribers:
            if sink.offer(event):
                delivered += 1
            else:
                broken.append(sink)
        if broken:
            self._reap(broken)
        return delivered

    def _reap(self, sinks):
        with self._lock:
            for sink in sinks:
                if sink in self._subscribers:
                    self._subscribers.remove(sink)
        for sink in sinks:
            try:
                sink.close()
            except Exception:
                pass

    @property
    def subscriber_count(self):
        with self._lock:
            live = [sink for sink in self._subscribers if sink.alive]
            self._subscribers = live
            return len(live)

    def endpoint(self):
        try:
            family = address_type(self.address)
        except Exception:
            family = None
        return {
            "status": "closed" if self._closed or self._listener is None else "listening",
            "address": self.address,
            "family": family,
            "subscriber_count": self.subscriber_count,
        }

    def close(self):
        self._closed = True
        self._wake_accept_loop()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except Exception:
                pass
        with self._lock:
            subscribers, self._subscribers = list(self._subscribers), []
        for sink in subscribers:
            try:
                sink.close()
            except Exception:
                pass

    def _wake_accept_loop(self):
        """Unblock a thread parked in listener.accept().

        multiprocessing listeners have no interruptible accept, and on Windows a
        parked accept keeps a live pipe instance alive after close(), so clients
        could still connect to a closed channel. A throwaway self-connect makes
        accept return so the loop can observe the closed flag and exit.
        """
        thread = self._thread
        if thread is None or not thread.is_alive() or self._listener is None:
            return
        try:
            conn = Client(self.address, authkey=self.authkey)
        except Exception:
            return
        try:
            conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.close()

    def _accept_loop(self):
        while not self._closed:
            listener = self._listener
            if listener is None:
                break
            try:
                conn = listener.accept()
            except Exception:
                break
            if self._closed:
                try:
                    conn.close()
                except Exception:
                    pass
                break
            # The ack goes out on the sink's own thread, in front of the queue, so a client that
            # connects and never reads cannot stall the accept loop either.
            sink = _SubscriberSink(conn, queue_size=self.queue_size)
            sink.offer({"type": SUBSCRIBED_ACK, "address": self.address})
            with self._lock:
                self._subscribers.append(sink)
            sink.start()


def _is_windows():
    import sys

    return sys.platform == "win32"
