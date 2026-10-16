"""Compatibility shim for :mod:`genericagent.agents.mailbox`."""
import sys
from genericagent.agents import mailbox as _impl
sys.modules[__name__] = _impl
