"""Compatibility entrypoint for :mod:`genericagent.core.app`."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("genericagent.core.app", run_name="__main__")
else:
    from genericagent.core import app as _impl
    sys.modules[__name__] = _impl
