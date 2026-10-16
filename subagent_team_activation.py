"""Compatibility shim for :mod:`genericagent.agents.team_activation`."""
import sys
from genericagent.agents import team_activation as _impl
sys.modules[__name__] = _impl
