"""写前必读：拒绝「没看过当前内容就覆盖」。

**问题**：`file_write` 的 schema 描述里写着 "Before overwriting an existing file,
read it first"，但 `do_file_write` 一个字都不校验——模型完全可以凭记忆覆盖一个
已被外部修改过的文件，把别人的改动冲掉。`file_patch` 同理，它比 `file_write`
更依赖「看到过 old_content」这个前提。

**对齐对象**：Claude Code 319（`docs/20261107-CC内置工具实现调研.md` §16.3 第 16 条）
的三条规则：

1. 从没读过 → 拒绝；
2. 读过但只读了片段（没到文件末尾） → 拒绝，要求读全；
3. 读过，且 mtime/size 未变 → 放行；mtime 变了但内容 sha256 相同 → 放行；
   mtime 变了且内容不同 → 拒绝，要求重读。

**为什么记录 sha256 而不只是 mtime**：`git checkout`、格式化器、`touch` 都会
动 mtime 而不改内容。只认 mtime 会把这类无风险操作也拦下来，用户很快就会
把这个检查当成噪音并绕过它——那样检查就失效了。

**为什么 append/prepend 不检查**：追加不需要旧内容，只要写入模式是追加，
「没读过当前内容」不构成风险。
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class ReadRecord:
    """一次成功读取留下的快照。"""

    realpath: str
    mtime_ns: int
    size: int
    sha256: str
    # 是否读到了文件末尾。只读了前 200 行不足以支撑「覆盖整个文件」。
    complete: bool


@dataclass(frozen=True)
class WriteVerdict:
    allowed: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason}


_ALLOW = WriteVerdict(True)


class ReadBeforeWriteRegistry:
    """进程内记录「哪些文件被完整读过」。

    刻意做成实例而不是模块级全局：workflow 子 agent 与主 agent 各自持有自己的
    读取历史，不能让一个 agent 的读取替另一个 agent 背书。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, ReadRecord] = {}

    @staticmethod
    def _key(path: str) -> str:
        try:
            return os.path.normcase(os.path.realpath(str(path)))
        except OSError:
            return os.path.normcase(str(path))

    @staticmethod
    def _fingerprint(path: str) -> tuple[int, int, str] | None:
        try:
            stat = os.stat(path)
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size, digest

    def record_read(self, path: str, *, complete: bool) -> None:
        fingerprint = self._fingerprint(path)
        if fingerprint is None:
            return
        mtime_ns, size, digest = fingerprint
        with self._lock:
            self._records[self._key(path)] = ReadRecord(
                realpath=os.path.realpath(str(path)),
                mtime_ns=mtime_ns,
                size=size,
                sha256=digest,
                complete=complete,
            )

    def forget(self, path: str) -> None:
        with self._lock:
            self._records.pop(self._key(path), None)

    def check_write(self, path: str, *, mode: str = "overwrite") -> WriteVerdict:
        """Decide whether ``path`` may be written now."""

        if str(mode or "").strip().lower() in {"append", "prepend"}:
            # Adding to a file does not depend on knowing its current contents.
            return _ALLOW
        if not os.path.exists(path):
            # Creating a new file cannot clobber anything.
            return _ALLOW

        with self._lock:
            record = self._records.get(self._key(path))
        if record is None:
            return WriteVerdict(
                False,
                f"{os.path.basename(str(path))} has not been read in this session. "
                f"Call file_read on it first so the write is based on its current content.",
            )
        if not record.complete:
            return WriteVerdict(
                False,
                f"Only part of {os.path.basename(str(path))} was read. Read the whole file "
                f"before overwriting it, otherwise the unread part will be lost.",
            )

        fingerprint = self._fingerprint(path)
        if fingerprint is None:
            return WriteVerdict(False, f"{os.path.basename(str(path))} could not be inspected before writing.")
        mtime_ns, size, digest = fingerprint
        if (mtime_ns, size) == (record.mtime_ns, record.size):
            return _ALLOW
        if digest == record.sha256:
            # mtime/size moved but the bytes are identical (touch, git checkout of
            # the same content, formatter no-op). Refreshing is enough.
            with self._lock:
                self._records[self._key(path)] = ReadRecord(
                    realpath=record.realpath,
                    mtime_ns=mtime_ns,
                    size=size,
                    sha256=digest,
                    complete=record.complete,
                )
            return _ALLOW
        return WriteVerdict(
            False,
            f"{os.path.basename(str(path))} changed on disk since it was read. "
            f"Re-read it with file_read before writing, so you do not overwrite those changes.",
        )


def record_read(registry: ReadBeforeWriteRegistry | None, path: str, *, complete: bool) -> None:
    """便利包装：registry 为空时什么也不做（兼容未注入的调用方）。"""

    if registry is not None:
        registry.record_read(path, complete=complete)
