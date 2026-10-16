"""Compatibility shim for :mod:`genericagent.agents.realtime_ipc`."""
import sys
from genericagent.agents import realtime_ipc as _impl
sys.modules[__name__] = _impl
