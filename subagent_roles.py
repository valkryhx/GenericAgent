"""Compatibility shim for :mod:`genericagent.agents.roles`."""
import sys
from genericagent.agents import roles as _impl
sys.modules[__name__] = _impl
