"""Compatibility shim for :mod:`genericagent.llm.client`."""
import sys
from genericagent.llm import client as _impl
sys.modules[__name__] = _impl
