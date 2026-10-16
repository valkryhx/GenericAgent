"""Compatibility shim for :mod:`genericagent.llm.config`."""
import sys
from genericagent.llm import config as _impl
sys.modules[__name__] = _impl
