from __future__ import annotations


_REALTIME_IPC_MODES = {"socket", "pipe", "event_server"}
_FILE_FALLBACK = "using durable file event bus fallback"


def normalize_ipc_metadata(ipc_mode=None, *, channel_factory=None):
    """Resolve the requested IPC mode into effective transport metadata.

    Realtime modes only become effective when ``channel_factory`` can open a
    resident channel; otherwise the durable file event bus stays the transport.
    The returned ``channel`` entry is a live object and must not be persisted.
    """
    requested = str(ipc_mode or "file").strip().lower() or "file"
    if requested == "file":
        return {
            "ipc_mode": "file",
            "effective_ipc_mode": "file",
            "ipc_fallback_reason": None,
            "ipc_endpoint": None,
            "channel": None,
        }
    if requested not in _REALTIME_IPC_MODES:
        return {
            "ipc_mode": requested,
            "effective_ipc_mode": "file",
            "ipc_fallback_reason": f"unknown IPC mode {requested}; {_FILE_FALLBACK}",
            "ipc_endpoint": None,
            "channel": None,
        }
    if channel_factory is None:
        return {
            "ipc_mode": requested,
            "effective_ipc_mode": "file",
            "ipc_fallback_reason": f"realtime IPC channel is not configured; {_FILE_FALLBACK}",
            "ipc_endpoint": None,
            "channel": None,
        }
    try:
        channel = channel_factory()
        channel.start()
        endpoint = channel.endpoint()
    except Exception as e:
        return {
            "ipc_mode": requested,
            "effective_ipc_mode": "file",
            "ipc_fallback_reason": f"realtime IPC channel failed to open ({type(e).__name__}: {e}); {_FILE_FALLBACK}",
            "ipc_endpoint": None,
            "channel": None,
        }
    return {
        "ipc_mode": requested,
        "effective_ipc_mode": requested,
        "ipc_fallback_reason": None,
        "ipc_endpoint": endpoint,
        "channel": channel,
    }
