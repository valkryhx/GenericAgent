"""Compatibility shim for :mod:`genericagent.core.permissions.runtime`."""
import sys
from genericagent.core.permissions import runtime as _impl
sys.modules[__name__] = _impl
