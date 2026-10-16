"""Compatibility shim for :mod:`genericagent.workflow.runtime`."""
import sys
from genericagent.workflow import runtime as _impl
sys.modules[__name__] = _impl
