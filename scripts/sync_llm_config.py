#!/usr/bin/env python3
"""Copy the source-checkout llm.yaml over the installed wheel's per-user copy.

Why this exists
---------------
GA resolves llm.yaml differently depending on how it was started:

  * source checkout  -> ``<repo>/llm.yaml``
  * installed wheel  -> ``%APPDATA%/GenericAgent/llm.yaml`` (``user_config_dir()``)

Both files feed the same index-based model selector. ``llm_no`` is a POSITION
in the profile list, not a stable identity, so the two copies have to agree on
membership *and* order. If the source copy gains one profile, every later index
shifts, and a subagent started by an installed GA resolves its parent's
``llm_no`` to a completely different profile -- the original symptom was a
subagent silently switching to 官方-ds-v41-1017 and failing on that provider's
credentials.

``genericagent.agents.manager`` pins GA_LLM_CONFIG for children so the parent
and child agree at runtime, but that does not help when the user is *developing*
in the source tree and testing with the installed ``ga``. Sync the files.

Usage
-----
    python scripts/sync_llm_config.py            # sync, keeping a backup
    python scripts/sync_llm_config.py --dry-run  # report only
    python scripts/sync_llm_config.py --check    # exit 1 if they differ

The script refuses to write a file that does not parse as YAML, so a botched
edit in the source tree can never silently become the user's working config.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _default_target() -> Path:
    """Resolve the installed wheel's per-user config path.

    Imported lazily so ``--help`` and tests work without importing the package.
    """
    from genericagent.paths import user_config_dir

    return user_config_dir() / "llm.yaml"


def _validate_yaml(path: Path) -> None:
    """Refuse to ship a config that does not parse."""
    import yaml

    try:
        with open(path, encoding="utf-8") as handle:
            yaml.safe_load(handle)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        raise ValueError(f"{path} 不是合法 YAML：{exc}") from exc


def sync_config(source: Path, target: Path, *, dry_run: bool = False) -> dict:
    """Copy *source* over *target*, keeping a timestamped backup when it changes.

    Returns a dict with ``changed``, ``source``, ``target`` and ``backup`` (empty
    string when no backup was written).

    A byte-identical target is left untouched. That matters beyond tidiness:
    agentmain hot-reloads llm.yaml when its mtime changes, so rewriting the same
    bytes would needlessly rebuild every LLM session.
    """
    source = Path(source)
    target = Path(target)

    if not source.is_file():
        raise FileNotFoundError(f"源文件不存在：{source}")
    _validate_yaml(source)

    new_bytes = source.read_bytes()
    old_bytes = target.read_bytes() if target.is_file() else None
    result = {
        "changed": new_bytes != old_bytes,
        "source": str(source),
        "target": str(target),
        "backup": "",
    }
    if not result["changed"] or dry_run:
        return result

    target.parent.mkdir(parents=True, exist_ok=True)
    if old_bytes is not None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"{target.name}.bak.{stamp}-sync")
        shutil.copy2(target, backup)
        result["backup"] = str(backup)

    target.write_bytes(new_bytes)
    return result


def _describe(result: dict, *, dry_run: bool, check: bool) -> str:
    if not result["changed"]:
        return f"已是最新：{result['target']}"
    if check:
        return f"配置不一致：\n  源  : {result['source']}\n  目标: {result['target']}"
    if dry_run:
        return (
            f"[dry-run] 将会覆盖：\n"
            f"  源  : {result['source']}\n"
            f"  目标: {result['target']}"
        )
    lines = [
        f"已同步：{result['source']}  ->  {result['target']}",
    ]
    if result["backup"]:
        lines.append(f"  备份: {result['backup']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把源码目录的 llm.yaml 同步到安装版使用的用户配置目录。",
    )
    parser.add_argument(
        "--source",
        default=str(REPO_ROOT / "llm.yaml"),
        help="源配置文件（默认：仓库根 llm.yaml）",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="目标配置文件（默认：用户配置目录 llm.yaml）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写入")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查；两者不一致时以退出码 1 结束（适合放进 pre-push 钩子）",
    )
    args = parser.parse_args(argv)

    source = Path(args.source).expanduser()
    target = Path(args.target).expanduser() if args.target else _default_target()

    try:
        result = sync_config(source, target, dry_run=args.dry_run or args.check)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    print(_describe(result, dry_run=args.dry_run, check=args.check))
    if args.check and result["changed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())