"""Compatibility shim for :mod:`genericagent.core.permissions.policy`."""
import sys
from genericagent.core.permissions import policy as _impl
sys.modules[__name__] = _impl
