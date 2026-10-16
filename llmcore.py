"""Compatibility shim for :mod:`genericagent.llm.core`."""
import sys
from genericagent.llm import core as _impl
sys.modules[__name__] = _impl
