"""Compatibility shim for :mod:`genericagent.agents.inbound_gate`."""
import sys
from genericagent.agents import inbound_gate as _impl
sys.modules[__name__] = _impl
