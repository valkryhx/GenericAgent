"""Compatibility shim for :mod:`genericagent.workflow.determinism`."""
import sys
from genericagent.workflow import determinism as _impl
sys.modules[__name__] = _impl
