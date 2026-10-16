"""Compatibility shim for :mod:`genericagent.workflow.permissions`."""
import sys
from genericagent.workflow import permissions as _impl
sys.modules[__name__] = _impl
