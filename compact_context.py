"""Compatibility shim for :mod:`genericagent.core.context`."""
import sys
from genericagent.core import context as _impl
sys.modules[__name__] = _impl
