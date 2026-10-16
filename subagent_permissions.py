"""Compatibility shim for :mod:`genericagent.agents.permissions`."""
import sys
from genericagent.agents import permissions as _impl
sys.modules[__name__] = _impl
