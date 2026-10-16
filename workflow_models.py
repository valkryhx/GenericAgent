"""Compatibility shim for :mod:`genericagent.workflow.models`."""
import sys
from genericagent.workflow import models as _impl
sys.modules[__name__] = _impl
