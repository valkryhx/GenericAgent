"""Compatibility shim for :mod:`genericagent.core.transcripts`."""
import sys
from genericagent.core import transcripts as _impl
sys.modules[__name__] = _impl
