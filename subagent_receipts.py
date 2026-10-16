"""Compatibility shim for :mod:`genericagent.agents.receipts`."""
import sys
from genericagent.agents import receipts as _impl
sys.modules[__name__] = _impl
