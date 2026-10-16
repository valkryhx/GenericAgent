"""Compatibility shim for :mod:`genericagent.workflow.tool_profiles`."""
import sys
from genericagent.workflow import tool_profiles as _impl
sys.modules[__name__] = _impl
