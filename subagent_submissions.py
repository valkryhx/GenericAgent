"""Compatibility shim for :mod:`genericagent.agents.submissions`."""
import sys
from genericagent.agents import submissions as _impl
sys.modules[__name__] = _impl
