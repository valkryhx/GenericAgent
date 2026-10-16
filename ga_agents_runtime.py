"""Compatibility shim for :mod:`genericagent.core.project_instructions`."""
import sys
from genericagent.core import project_instructions as _impl
sys.modules[__name__] = _impl
