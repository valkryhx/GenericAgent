"""Compatibility shim for the Ink JSONL bridge.

Canonical implementation lives at ``frontends.ink.bridge``.  The module is
aliased in ``sys.modules`` so old imports and patch targets keep pointing at the
same module object.
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from frontends.ink import bridge as _bridge  # noqa: E402

sys.modules[__name__] = _bridge

if __name__ == "__main__":
    raise SystemExit(_bridge.main())
