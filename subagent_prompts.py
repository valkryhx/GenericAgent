"""Compatibility shim for :mod:`genericagent.agents.prompts`."""
import sys
from genericagent.agents import prompts as _impl
sys.modules[__name__] = _impl
