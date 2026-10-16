"""Compatibility shim for :mod:`genericagent.agents.worktree`."""
import sys
from genericagent.agents import worktree as _impl
sys.modules[__name__] = _impl
