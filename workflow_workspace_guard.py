"""Best-effort in-process filesystem guard for workflow Python code_run children."""
from __future__ import annotations

import builtins
import io
import os
import shutil
import subprocess
from pathlib import Path


def install(root: str) -> None:
    workspace = Path(root).resolve()

    def resolve(value):
        if isinstance(value, int):
            return value
        path = Path(value)
        candidate = path.resolve() if path.is_absolute() else (workspace / path).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise PermissionError(f"workflow code path outside workspace: {value}") from exc
        return str(candidate)

    original_open = builtins.open
    original_io_open = io.open
    original_os_open = os.open
    original_remove = os.remove
    original_unlink = os.unlink
    original_mkdir = os.mkdir
    original_makedirs = os.makedirs
    original_rename = os.rename
    original_replace = os.replace
    original_rmtree = shutil.rmtree

    def guarded_open(file, *args, **kwargs):
        return original_open(resolve(file), *args, **kwargs)

    def guarded_io_open(file, *args, **kwargs):
        return original_io_open(resolve(file), *args, **kwargs)

    def guarded_os_open(file, *args, **kwargs):
        return original_os_open(resolve(file), *args, **kwargs)

    def guarded_unary(fn):
        return lambda file, *args, **kwargs: fn(resolve(file), *args, **kwargs)

    def guarded_mkdir(path, *args, **kwargs):
        return original_mkdir(resolve(path), *args, **kwargs)

    def guarded_makedirs(name, *args, **kwargs):
        return original_makedirs(resolve(name), *args, **kwargs)

    def guarded_move(src, dst, *args, **kwargs):
        return original_rename(resolve(src), resolve(dst), *args, **kwargs)

    def guarded_replace(src, dst, *args, **kwargs):
        return original_replace(resolve(src), resolve(dst), *args, **kwargs)

    def guarded_rmtree(path, *args, **kwargs):
        return original_rmtree(resolve(path), *args, **kwargs)

    builtins.open = guarded_open
    io.open = guarded_io_open
    os.open = guarded_os_open
    os.remove = guarded_unary(original_remove)
    os.unlink = guarded_unary(original_unlink)
    os.mkdir = guarded_mkdir
    os.makedirs = guarded_makedirs
    os.rename = guarded_move
    os.replace = guarded_replace
    shutil.rmtree = guarded_rmtree

    def deny_process(*args, **kwargs):
        raise PermissionError("workflow code_run subprocess is disabled by workspace guard")

    subprocess.Popen = deny_process
    subprocess.run = deny_process
    subprocess.call = deny_process
    subprocess.check_call = deny_process
    subprocess.check_output = deny_process
