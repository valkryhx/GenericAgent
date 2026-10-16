"""Compatibility shim for :mod:`genericagent.agents.state`."""
import sys
from genericagent.agents import state as _impl
sys.modules[__name__] = _impl
