"""Compatibility shim for :mod:`genericagent.core.skills`."""
import sys
from genericagent.core import skills as _impl
sys.modules[__name__] = _impl
