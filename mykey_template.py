"""Compatibility shim for :mod:`genericagent.llm.templates.mykey_template`."""
import sys
from genericagent.llm.templates import mykey_template as _impl
sys.modules[__name__] = _impl
