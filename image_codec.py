"""Compatibility shim for :mod:`genericagent.media.image_codec`."""
import sys
from genericagent.media import image_codec as _impl
sys.modules[__name__] = _impl
