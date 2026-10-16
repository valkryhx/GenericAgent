"""Compatibility shim for :mod:`genericagent.core.loop`."""
import sys
from genericagent.core import loop as _impl
sys.modules[__name__] = _impl
