"""Compatibility shim for :mod:`genericagent.workflow.child_agent`."""
import sys
from genericagent.workflow import child_agent as _impl
sys.modules[__name__] = _impl
