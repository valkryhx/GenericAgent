"""Compatibility shim for :mod:`genericagent.agents.tasks`."""
import sys
from genericagent.agents import tasks as _impl
sys.modules[__name__] = _impl
