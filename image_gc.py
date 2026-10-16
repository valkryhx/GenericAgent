"""Compatibility shim for :mod:`genericagent.media.image_gc`."""
import sys
from genericagent.media import image_gc as _impl
sys.modules[__name__] = _impl
