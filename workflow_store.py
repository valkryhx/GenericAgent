"""Compatibility shim for :mod:`genericagent.workflow.store`."""
import sys
from genericagent.workflow import store as _impl
sys.modules[__name__] = _impl
