"""Compatibility shim for the pre-P6 top-level frontend path."""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from frontends._compat import alias_module

alias_module(__name__, "frontends.web.stapp2")
