"""Compatibility shim for :mod:`genericagent.agents.notifications`."""
import sys
from genericagent.agents import notifications as _impl
sys.modules[__name__] = _impl
