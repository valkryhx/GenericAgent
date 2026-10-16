"""Compatibility shim for :mod:`genericagent.workflow.check_adapters`."""
import sys
from genericagent.workflow import check_adapters as _impl
sys.modules[__name__] = _impl
