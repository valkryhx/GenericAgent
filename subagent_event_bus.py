"""Compatibility shim for :mod:`genericagent.agents.event_bus`."""
import sys
from genericagent.agents import event_bus as _impl
sys.modules[__name__] = _impl
