"""Compatibility shim for :mod:`genericagent.agents.transcript`."""
import sys
from genericagent.agents import transcript as _impl
sys.modules[__name__] = _impl
