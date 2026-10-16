"""Compatibility shim for :mod:`genericagent.core.tokens`."""
import sys
from genericagent.core import tokens as _impl
sys.modules[__name__] = _impl
