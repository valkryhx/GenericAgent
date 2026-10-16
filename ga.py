"""Compatibility shim for :mod:`genericagent.core.handler`."""
import sys
from genericagent.core import handler as _impl
sys.modules[__name__] = _impl
