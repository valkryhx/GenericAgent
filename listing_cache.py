"""Small, self-healing JSON cache for the ``/resume`` listing scans.

The picker only needs a few fields per session file (id, preview, rounds, the
user texts used to hide a legacy log a transcript already covers), but the only
way to get them is to parse the file.  Listing walks a few thousand files on
every open, and the Ink bridge calls it twice (open the picker, then resume by
index), so each file's summary is cached and keyed on ``(mtime, size)``: any
append changes both, so a stale entry can never be served.

Caching is best-effort by design.  A missing, corrupt, or unwritable cache costs
time and nothing else -- callers always fall back to a real scan, so the listing
can never be wrong because of it.  Writes are atomic (tmp file + ``os.replace``)
so two GA processes sharing a directory cannot leave a half-written file behind.
"""

from __future__ import annotations

import json
import os
import tempfile

VERSION = 1


def load(path):
    """Return the cached ``{key: entry}`` map, or ``{}`` if unusable."""
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return {}
    if not isinstance(payload, dict) or payload.get("version") != VERSION:
        return {}
    entries = payload.get("entries")
    return entries if isinstance(entries, dict) else {}


def save(path, entries):
    """Atomically persist ``{key: entry}``; never raises."""
    payload = {"version": VERSION, "entries": entries}
    directory = os.path.dirname(path) or "."
    tmp = None
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".listing_cache.", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
        tmp = None
    except Exception:
        return
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def is_fresh(entry, stat):
    """True when ``entry`` was produced from exactly this file version."""
    return (isinstance(entry, dict)
            and entry.get("mtime") == stat.st_mtime
            and entry.get("size") == stat.st_size)
