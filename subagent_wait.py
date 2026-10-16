"""Compatibility shim for :mod:`genericagent.agents.wait`."""
import sys
from genericagent.agents import wait as _impl
sys.modules[__name__] = _impl
