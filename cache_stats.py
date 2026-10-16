"""Compatibility shim for :mod:`genericagent.core.cache_stats`."""
import sys
from genericagent.core import cache_stats as _impl
sys.modules[__name__] = _impl
