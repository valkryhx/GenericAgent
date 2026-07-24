"""GA 图片临时目录 GC（Slice D3）。

只清理 **GA 自产** 的 clipboard/upload 图片，绝不碰用户原图或 sessions。

对齐：
- Claude Code：可扫 image-cache 目录 + MAX ~200 条
- Codex：clipboard 短生命周期落盘

根目录解析与 frontends/ink-ui/src/clipboardImage.ts 的 resolveGaImageRoot 一致：
1. env GA_IMAGE_TEMP
2. {repo}/temp/ga-images
3. fallback {tmpdir}/ga-images
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

RETAIN_DAYS = 7
MAX_FILES_PER_DIR = 200
# 仅这些前缀视为 GA 自产图片（防止误删用户丢进 ga-images 的文件）
OWNED_PREFIXES = ("clipboard-", "upload-")
OWNED_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


@dataclass
class GcResult:
    roots: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    deleted_dirs: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def deleted_count(self) -> int:
        return len(self.deleted_files)


def repo_root() -> Path:
    return Path(__file__).resolve().parent


def resolve_ga_image_root(*, prefer_repo: bool = True) -> Path:
    """与 TS resolveGaImageRoot 同规则。"""
    env = (os.environ.get("GA_IMAGE_TEMP") or "").strip()
    if env:
        p = Path(env).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p.resolve()
    if prefer_repo:
        candidate = repo_root() / "temp" / "ga-images"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate.resolve()
        except OSError:
            pass
    fallback = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp") / "ga-images"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback.resolve()


def legacy_tmpdir_ga_images() -> Path:
    return (Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp") / "ga-images").resolve()


def is_owned_image_file(path: Path) -> bool:
    name = path.name.lower()
    if not any(name.startswith(p) for p in OWNED_PREFIXES):
        return False
    return any(name.endswith(s) for s in OWNED_SUFFIXES)


def _iter_owned_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            p = Path(dirpath) / fn
            if is_owned_image_file(p):
                yield p


def _safe_unlink(path: Path, result: GcResult) -> None:
    try:
        path.unlink(missing_ok=True)
        result.deleted_files.append(str(path))
    except OSError as e:
        result.errors.append(f"unlink {path}: {e}")


def _prune_empty_dirs(root: Path, result: GcResult) -> None:
    if not root.is_dir():
        return
    # bottom-up
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p.resolve() == root.resolve():
            continue
        try:
            if not any(p.iterdir()):
                p.rmdir()
                result.deleted_dirs.append(str(p))
        except OSError:
            pass


def gc_ga_images_root(
    root: Path | str,
    *,
    retain_days: float = RETAIN_DAYS,
    max_per_dir: int = MAX_FILES_PER_DIR,
    now: float | None = None,
) -> GcResult:
    """清理单个 ga-images 根：过期 + 每目录条数上限。"""
    result = GcResult(roots=[str(root)])
    root_p = Path(root)
    if not root_p.is_dir():
        return result

    now_ts = time.time() if now is None else now
    cutoff = now_ts - float(retain_days) * 86400.0

    # 1) 过期
    for p in list(_iter_owned_files(root_p)):
        try:
            mtime = p.stat().st_mtime
        except OSError as e:
            result.errors.append(f"stat {p}: {e}")
            continue
        if mtime < cutoff:
            _safe_unlink(p, result)
        else:
            result.skipped.append(str(p))

    # 2) 每目录条数上限（含根自身）
    dirs: set[Path] = {root_p}
    for p in _iter_owned_files(root_p):
        dirs.add(p.parent)
    for d in dirs:
        try:
            files = [x for x in d.iterdir() if x.is_file() and is_owned_image_file(x)]
        except OSError as e:
            result.errors.append(f"list {d}: {e}")
            continue
        if len(files) <= max_per_dir:
            continue
        files.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0)
        overflow = len(files) - max_per_dir
        for p in files[:overflow]:
            # 可能已在过期阶段删掉
            if p.exists():
                _safe_unlink(p, result)

    _prune_empty_dirs(root_p, result)
    return result


def gc_ga_images(
    *,
    retain_days: float = RETAIN_DAYS,
    max_per_dir: int = MAX_FILES_PER_DIR,
    include_legacy_tmpdir: bool = True,
    now: float | None = None,
) -> GcResult:
    """启动时调用：清当前根 + 可选旧 tmpdir 根。"""
    merged = GcResult()
    roots: list[Path] = [resolve_ga_image_root()]
    if include_legacy_tmpdir:
        legacy = legacy_tmpdir_ga_images()
        # 避免与当前根是同一路径时重复扫
        if legacy.resolve() != roots[0].resolve():
            roots.append(legacy)

    for root in roots:
        part = gc_ga_images_root(
            root,
            retain_days=retain_days,
            max_per_dir=max_per_dir,
            now=now,
        )
        merged.roots.extend(part.roots)
        merged.deleted_files.extend(part.deleted_files)
        merged.deleted_dirs.extend(part.deleted_dirs)
        merged.skipped.extend(part.skipped)
        merged.errors.extend(part.errors)
    return merged


def maybe_gc_ga_images_on_startup(quiet: bool = True) -> GcResult | None:
    """best-effort；失败不阻断启动。"""
    try:
        result = gc_ga_images()
        if not quiet and (result.deleted_count or result.errors):
            print(
                f"[image_gc] deleted={result.deleted_count} "
                f"dirs={len(result.deleted_dirs)} errors={len(result.errors)} roots={result.roots}"
            )
        return result
    except Exception as e:  # pragma: no cover
        if not quiet:
            print(f"[image_gc] skipped: {e}")
        return None


if __name__ == "__main__":
    r = gc_ga_images()
    print(f"roots={r.roots}")
    print(f"deleted={r.deleted_count}")
    for p in r.deleted_files[:20]:
        print(" -", p)
    if r.errors:
        print("errors:", r.errors)
