"""Compatibility shim for :mod:`genericagent.web.tmwebdriver`."""
import sys
from genericagent.web import tmwebdriver as _impl
sys.modules[__name__] = _impl
