"""Compatibility shim for :mod:`genericagent.core.redaction`."""
import sys
from genericagent.core import redaction as _impl
sys.modules[__name__] = _impl
