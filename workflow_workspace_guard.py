"""Compatibility shim for :mod:`genericagent.workflow.workspace_guard`."""
import sys
from genericagent.workflow import workspace_guard as _impl
sys.modules[__name__] = _impl
