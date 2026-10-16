"""Compatibility shim for :mod:`genericagent.workflow.llm`."""
import sys
from genericagent.workflow import llm as _impl
sys.modules[__name__] = _impl
