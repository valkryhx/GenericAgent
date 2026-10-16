"""Helpers for frontend compatibility shims.

The compatibility modules kept at the old top-level paths must preserve both
module identity and direct execution.  ``alias_module`` does that without
copying implementation code into the shim.
"""
from __future__ import annotations

import importlib
import runpy
import sys


def alias_module(shim_name: str, target_name: str) -> None:
    """Alias ``shim_name`` to ``target_name`` or run it as ``__main__``."""
    if shim_name == "__main__":
        runpy.run_module(target_name, run_name="__main__")
        return
    sys.modules[shim_name] = importlib.import_module(target_name)
