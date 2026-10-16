"""Compatibility shim for :mod:`genericagent.workflow.script_validation`."""
import sys
from genericagent.workflow import script_validation as _impl
sys.modules[__name__] = _impl
