"""Compatibility shim for :mod:`genericagent.agents.swarm`."""
import sys
from genericagent.agents import swarm as _impl
sys.modules[__name__] = _impl
