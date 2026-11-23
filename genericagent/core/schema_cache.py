"""Cache only packaged schema assets. Dynamic tools are resolved on each turn."""

import copy
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=8)
def _read_asset(path, platform, version):
    with open(path, encoding='utf-8') as file:
        text = file.read()
    return json.loads(text if platform == 'nt' else text.replace('powershell', 'bash'))


def load_static_tool_schema(path, platform):
    path = Path(path).resolve()
    stat = path.stat()
    version = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
    return copy.deepcopy(_read_asset(path, platform, version))
