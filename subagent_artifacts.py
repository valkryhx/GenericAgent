"""Compatibility shim for :mod:`genericagent.agents.artifacts`."""
import sys
from genericagent.agents import artifacts as _impl
sys.modules[__name__] = _impl
