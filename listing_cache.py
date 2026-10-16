"""Compatibility shim for :mod:`genericagent.core.listing_cache`."""
import sys
from genericagent.core import listing_cache as _impl
sys.modules[__name__] = _impl
