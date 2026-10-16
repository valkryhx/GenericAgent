"""Compatibility shim for :mod:`genericagent.agents.ipc`."""
import sys
from genericagent.agents import ipc as _impl
sys.modules[__name__] = _impl
