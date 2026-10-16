"""Compatibility shim for :mod:`genericagent.mcp.runtime`."""
import sys
from genericagent.mcp import runtime as _impl
sys.modules[__name__] = _impl
