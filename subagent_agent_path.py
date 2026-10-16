"""Compatibility shim for :mod:`genericagent.agents.agent_path`."""
import sys
from genericagent.agents import agent_path as _impl
sys.modules[__name__] = _impl
