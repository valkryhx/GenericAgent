"""Compatibility shim for :mod:`genericagent.workflow.workspace`."""
import sys
from genericagent.workflow import workspace as _impl
sys.modules[__name__] = _impl
