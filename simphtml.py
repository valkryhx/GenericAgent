"""Compatibility shim for :mod:`genericagent.web.simphtml`."""
import sys
from genericagent.web import simphtml as _impl
sys.modules[__name__] = _impl
