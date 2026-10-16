"""Compatibility shim for :mod:`genericagent.workflow.scheduler`."""
import sys
from genericagent.workflow import scheduler as _impl
sys.modules[__name__] = _impl
