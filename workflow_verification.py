"""Compatibility shim for :mod:`genericagent.workflow.verification`."""
import sys
from genericagent.workflow import verification as _impl
sys.modules[__name__] = _impl
