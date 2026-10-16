"""Compatibility shim for :mod:`genericagent.core.control.workflow`."""
import sys
from genericagent.core.control import workflow as _impl
sys.modules[__name__] = _impl
