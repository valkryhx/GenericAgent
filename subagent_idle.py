"""Compatibility shim for :mod:`genericagent.agents.idle`."""
import sys
from genericagent.agents import idle as _impl
sys.modules[__name__] = _impl
