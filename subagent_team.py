"""Compatibility shim for :mod:`genericagent.agents.team`."""
import sys
from genericagent.agents import team as _impl
sys.modules[__name__] = _impl
