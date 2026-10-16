"""
ga_cli - GenericAgent CLI 命令包

作为包导入时，从 :mod:`genericagent.core.handler` 重新导出稳定核心符号。
以 ``python -m ga_cli`` 运行时，进入 CLI 命令模式。
"""
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from genericagent.core import handler as _handler

_EXPORT_NAMES = [
    'GenericAgentHandler', 'smart_format', 'get_global_memory',
    'format_error', 'consume_file', 'code_run', 'script_dir',
    'BaseHandler', 'StepOutcome',
]
for _name in _EXPORT_NAMES:
    if hasattr(_handler, _name):
        globals()[_name] = getattr(_handler, _name)
__all__ = [n for n in _EXPORT_NAMES if hasattr(_handler, n)]
