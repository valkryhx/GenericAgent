"""Compatibility shim for :mod:`genericagent.agents.manager`."""
import sys
from genericagent.agents import manager as _impl
sys.modules[__name__] = _impl
