"""Compatibility shim for :mod:`genericagent.core.tool_results.limits`."""
import sys
from genericagent.core.tool_results import limits as _impl
sys.modules[__name__] = _impl
