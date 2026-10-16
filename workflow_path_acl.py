"""Compatibility shim for :mod:`genericagent.workflow.path_acl`."""
import sys
from genericagent.workflow import path_acl as _impl
sys.modules[__name__] = _impl
