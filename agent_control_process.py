"""Compatibility shim for :mod:`genericagent.core.control.process`."""
import sys
from genericagent.core.control import process as _impl
sys.modules[__name__] = _impl
