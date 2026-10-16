"""Compatibility shim for :mod:`genericagent.agents.registry`."""
import sys
from genericagent.agents import registry as _impl
sys.modules[__name__] = _impl
