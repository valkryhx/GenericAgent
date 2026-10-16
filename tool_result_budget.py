"""Compatibility shim for :mod:`genericagent.core.tool_results.budget`."""
import sys
from genericagent.core.tool_results import budget as _impl
sys.modules[__name__] = _impl
