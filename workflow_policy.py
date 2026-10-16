"""Compatibility shim for :mod:`genericagent.workflow.policy`."""
import sys
from genericagent.workflow import policy as _impl
sys.modules[__name__] = _impl
