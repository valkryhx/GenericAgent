"""Compatibility shim for :mod:`genericagent.workflow.controller`."""
import sys
from genericagent.workflow import controller as _impl
sys.modules[__name__] = _impl
