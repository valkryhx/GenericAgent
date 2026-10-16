"""Compatibility shim for :mod:`genericagent.core.models`."""
import sys
from genericagent.core import models as _impl
sys.modules[__name__] = _impl
