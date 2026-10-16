"""Compatibility shim for :mod:`genericagent.core.control.control`."""
import sys
from genericagent.core.control import control as _impl
sys.modules[__name__] = _impl
