"""Compatibility shim for :mod:`genericagent.agents.capabilities`."""
import sys
from genericagent.agents import capabilities as _impl
sys.modules[__name__] = _impl
