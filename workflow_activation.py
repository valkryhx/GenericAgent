"""Compatibility shim for :mod:`genericagent.workflow.activation`."""
import sys
from genericagent.workflow import activation as _impl
sys.modules[__name__] = _impl
