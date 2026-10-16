"""Compatibility shim for :mod:`genericagent.llm.templates.mykey_template_en`."""
import sys
from genericagent.llm.templates import mykey_template_en as _impl
sys.modules[__name__] = _impl
