"""探针执行期间的源码护栏。

## 为什么需要这个（真实事故，2026-11-14）

用自然语言跑 `ask_q_jsonl_probe.py` 验收 ask_user_question 时，探针确实
撞出了提问卡、也确实把答案送到了模型 —— 但模型在问完之后**顺手改了源码**：

    genericagent/core/pending_input.py   +87 行

那次是我事后 `git checkout --` 才救回来的。事后再看 `git status` 干净，
**不等于**探针执行期间是干净的；等到发现时，如果已经 commit，污染就进了历史。

探针的职责是"观察真实行为"，它的产物应该是 captures 里的 jsonl，
**绝不能是源码改动**。所以护栏要满足三件事：

1. 探针跑完**立刻**比对工作树，不依赖人记得去查；
2. 只还原**开始时干净、跑完变脏**的文件 —— 开始时就脏的是用户自己没提交的
   工作，那不是探针的产物，擅自 checkout 会毁掉用户的活；
3. 还原之前先把 diff 存盘 —— 还原不能等于"消灭证据"。

## 判定方式

快照 = 所有 tracked 文件的 sha256 + 开始时 `git status` 显示的脏文件集合。
跑完重算 sha256，差集就是"探针期间变了"。

- `changed - dirty_at_start` → 探针改的，**还原**
- `changed & dirty_at_start` → 无法区分是谁改的，**只报告**（补丁已存盘）
- 新增未跟踪文件 → **只报告**（可能是探针生成的临时文件，但删除风险更高）

用 sha256 而不是 `git stash create`：后者在无改动时返回空串、且不含未跟踪
文件，判定边界更绕；直接比内容最直白。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROBE_LIB = REPO / "tests" / "probes" / "python"
if str(PROBE_LIB) not in sys.path:
    sys.path.insert(0, str(PROBE_LIB))

from _workspace_guard import (  # noqa: E402
    CONTAMINATED_EXIT,
    WorkspaceGuard,
    finish,
    guarded_main,
    start,
)


# --------------------------------------------------------------------------
# 脚手架：每个测试一个临时 git 仓库，绝不碰真仓库
# --------------------------------------------------------------------------

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    # 本机 core.autocrlf=true，临时仓库里关掉，免得行尾归一化冒充"内容变了"
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.autocrlf",
    "GIT_CONFIG_VALUE_0": "false",
}


def _git(root: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(GIT_ENV)
    proc = subprocess.run(
        ["git", *args], cwd=str(root), env=env,
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


class TempRepo:
    def __init__(self, root: Path) -> None:
        self.root = root
        _git(root, "init", "-q")
        _git(root, "config", "core.autocrlf", "false")

    def write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def read(self, rel: str) -> str:
        with open(self.root / rel, encoding="utf-8", newline="") as f:
            return f.read()

    def commit(self, msg: str = "init") -> None:
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", msg)


def _repo(tmp: Path) -> TempRepo:
    r = TempRepo(tmp)
    r.write("src/app.py", "print('hello')\n")
    r.write("src/util.py", "VALUE = 1\n")
    r.write("README.md", "readme\n")
    r.commit()
    return r


# --------------------------------------------------------------------------
# 核心：探针改源码 → 必须还原
# --------------------------------------------------------------------------


class RevertTest(unittest.TestCase):
    def test_probe_modification_is_reverted(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            guard = WorkspaceGuard(str(repo.root))
            guard.snapshot()

            # 「探针」顺手改了源码
            repo.write("src/app.py", "print('hello')\n# model added this\n")

            report = guard.verify()
            self.assertEqual(["src/app.py"], report.changed)
            self.assertEqual("print('hello')\n", repo.read("src/app.py"))
            # 还原了，但污染确实发生过 —— 必须让探针看到，否则下次照样悄悄发生
            self.assertFalse(report.ok, "还原成功不等于没发生，探针应当非 0 退出")
            self.assertIn("src/app.py", report.summary())

    def test_untouched_files_are_not_touched(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            before = repo.read("src/util.py")
            guard = WorkspaceGuard(str(repo.root))
            guard.snapshot()
            report = guard.verify()
            self.assertEqual([], report.changed)
            self.assertEqual(before, repo.read("src/util.py"))

    def test_deleted_file_is_restored(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            guard = WorkspaceGuard(str(repo.root))
            guard.snapshot()

            os.remove(repo.root / "src" / "util.py")

            report = guard.verify()
            self.assertEqual(["src/util.py"], report.deleted)
            self.assertTrue((repo.root / "src" / "util.py").exists())
            self.assertEqual("VALUE = 1\n", repo.read("src/util.py"))


# --------------------------------------------------------------------------
# 边界：用户自己的未提交改动不能被探针护栏吃掉
# --------------------------------------------------------------------------


class PreExistingDirtyTest(unittest.TestCase):
    def test_dirty_at_start_is_reported_not_reverted(self):
        """开始就脏的文件，探针又改了 —— 分不清谁改的，绝不能 checkout。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            # 用户自己在探针开始前就改了，还没提交
            repo.write("src/util.py", "VALUE = 2  # my work in progress\n")

            guard = WorkspaceGuard(str(repo.root))
            guard.snapshot()

            # 探针在此之上又追加
            repo.write("src/util.py", "VALUE = 2  # my work in progress\n# probe junk\n")

            report = guard.verify()
            self.assertEqual(["src/util.py"], report.dirty_before)
            self.assertEqual([], report.changed, "已在脏清单里的不该算进 changed")
            self.assertEqual(["src/util.py"], report.suspect, "已脏又被改，必须报 suspect")
            self.assertFalse(report.ok)
            # 用户的工作 + 探针的追加都还在（没被 checkout 抹掉）
            self.assertIn("my work in progress", repo.read("src/util.py"))

    def test_dirty_at_start_untouched_is_not_reported(self):
        """开始就脏、探针没碰 —— 那是用户的活，不该算进任何告警。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            repo.write("src/util.py", "VALUE = 2\n")
            guard = WorkspaceGuard(str(repo.root))
            guard.snapshot()
            report = guard.verify()
            self.assertEqual(["src/util.py"], report.dirty_before)
            self.assertEqual([], report.changed)
            self.assertEqual([], report.suspect)
            self.assertEqual([], report.created)
            self.assertTrue(report.ok, "用户自己的未提交改动不该让探针失败")
            # 内容没被动
            self.assertEqual("VALUE = 2\n", repo.read("src/util.py"))

    def test_suspect_change_gets_a_patch_too(self):
        """分不清改动的来源时，更要留证据 —— 人得能自己看。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            captures = Path(td) / "captures"
            repo.write("src/util.py", "VALUE = 2\n")
            guard = WorkspaceGuard(str(repo.root), capture_dir=str(captures))
            guard.snapshot()
            repo.write("src/util.py", "VALUE = 3\n")
            report = guard.verify()
            self.assertTrue(report.suspect)
            self.assertTrue(report.patch_path, "suspect 也要存补丁")
            self.assertIn("VALUE = 3", Path(report.patch_path).read_text(encoding="utf-8"))

    def test_new_untracked_file_is_reported_not_deleted(self):
        """探针生成的新文件：报告，但不删（删错了更贵）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            guard = WorkspaceGuard(str(repo.root))
            guard.snapshot()

            repo.write("src/probe_scratch.py", "# generated\n")

            report = guard.verify()
            self.assertEqual(["src/probe_scratch.py"], report.created)
            self.assertFalse(report.ok, "新增未跟踪文件应当让人知道")
            self.assertTrue(
                (repo.root / "src" / "probe_scratch.py").exists(),
                "护栏不该删文件",
            )


# --------------------------------------------------------------------------
# 证据保全：还原前先存 diff
# --------------------------------------------------------------------------


class PatchTest(unittest.TestCase):
    def test_patch_is_saved_before_reverting(self):
        """还原不能等于消灭证据 —— 补丁必须落盘且含被还原的内容。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            captures = Path(td) / "captures"
            guard = WorkspaceGuard(str(repo.root), capture_dir=str(captures))
            guard.snapshot()

            repo.write("src/app.py", "print('changed by model')\n")

            report = guard.verify()
            self.assertTrue(report.patch_path, "没有写出补丁文件")
            patch = Path(report.patch_path).read_text(encoding="utf-8")
            self.assertIn("changed by model", patch)
            self.assertIn("src/app.py", patch)

    def test_no_patch_when_nothing_changed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            captures = Path(td) / "captures"
            guard = WorkspaceGuard(str(repo.root), capture_dir=str(captures))
            guard.snapshot()
            report = guard.verify()
            self.assertEqual("", report.patch_path, "没改动就不该产生补丁噪音")


# --------------------------------------------------------------------------
# 接线：探针抛异常也必须走到校验
# --------------------------------------------------------------------------


class ContextManagerTest(unittest.TestCase):
    def test_verify_runs_even_when_probe_raises(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            with self.assertRaises(RuntimeError):
                with WorkspaceGuard(str(repo.root)):
                    repo.write("src/app.py", "print('boom')\n")
                    raise RuntimeError("probe blew up")
            self.assertEqual("print('hello')\n", repo.read("src/app.py"))

    def test_snapshot_is_taken_automatically_on_enter(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            with WorkspaceGuard(str(repo.root)):
                repo.write("src/app.py", "print('x')\n")
            self.assertEqual("print('hello')\n", repo.read("src/app.py"))

    def test_report_exposes_ok_flag_for_the_caller(self):
        """探针要能据此决定 return code。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            with WorkspaceGuard(str(repo.root)) as guard:
                repo.write("src/util.py", "VALUE = 2\n")
            self.assertFalse(guard.report.ok)
            self.assertIn("src/util.py", guard.report.summary())


class RepoHygieneTest(unittest.TestCase):
    """护栏相关文件本身必须符合仓库的 LF 铁律（AGENTS 项目规范 §1）。

    踩过的坑：`Path.write_text()` 在 Windows 上把 `\\n` 翻成 `\\r\\n`，
    于是「反向确认脚本」每次跑完都把被测文件的 257 个 CR 带回来 ——
    脚本声称还原了，实际上悄悄改坏了行尾。
    """

    def test_guard_files_are_lf(self):
        files = [
            PROBE_LIB / "_workspace_guard.py",
            PROBE_LIB / "_mutate_workspace_guard.py",
            PROBE_LIB / "ask_q_jsonl_probe.py",
        ]
        for f in files:
            with self.subTest(file=f.name):
                data = f.read_bytes()
                self.assertEqual(
                    0, data.count(b"\r"), f"{f.name} 含 CR，应为纯 LF"
                )

    def test_mutate_script_writes_lf(self):
        """反向确认脚本写回时必须指定 newline，否则会把 CRLF 带回来。"""
        src = (PROBE_LIB / "_mutate_workspace_guard.py").read_text(encoding="utf-8")
        self.assertIn('newline="\\n"', src, "写回文件未指定 newline=，Windows 上会翻成 CRLF")


class GuardedMainTest(unittest.TestCase):
    """`guarded_main` 装饰器：任何探针一行套上护栏。

    推广用（仓库里 80+ 个探针，逐个手写 with 不现实）：

        @guarded_main(ROOT)
        def main() -> int:
            ...
        if __name__ == "__main__":
            sys.exit(main())
    """

    def _root(self, td):
        repo = _repo(Path(td))
        return repo

    def test_clean_run_returns_inner_code(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = self._root(td)

            @guarded_main(str(repo.root))
            def probe() -> int:
                return 0

            self.assertEqual(0, probe())

    def test_contamination_overrides_the_inner_return_code(self):
        """探针自己返回 0，但改了源码 —— 必须返回 2，不能让它冒充成功。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = self._root(td)

            @guarded_main(str(repo.root))
            def probe() -> int:
                repo.write("src/app.py", "print('model was here')\n")
                return 0

            self.assertEqual(2, probe())
            self.assertEqual("print('hello')\n", repo.read("src/app.py"))

    def test_inner_failure_code_is_preserved_when_clean(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = self._root(td)

            @guarded_main(str(repo.root))
            def probe() -> int:
                return 1

            self.assertEqual(1, probe(), "干净时不该改写探针自己的退出码")

    def test_contamination_wins_over_inner_failure_code(self):
        """两者都发生时报 2 —— 污染比探针自己的失败更需要被看见。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = self._root(td)

            @guarded_main(str(repo.root))
            def probe() -> int:
                repo.write("src/util.py", "VALUE = 9\n")
                return 1

            self.assertEqual(2, probe())

    def test_exception_still_verifies_then_propagates(self):
        """探针崩了也要校验，而且不能把异常吞掉。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = self._root(td)

            @guarded_main(str(repo.root))
            def probe() -> int:
                repo.write("src/app.py", "print('then blew up')\n")
                raise RuntimeError("probe crashed")

            with self.assertRaises(RuntimeError):
                probe()
            self.assertEqual("print('hello')\n", repo.read("src/app.py"))

    def test_args_are_forwarded(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = self._root(td)

            @guarded_main(str(repo.root))
            def probe(a, b=0) -> int:
                return a + b

            self.assertEqual(3, probe(1, b=2))

    def test_report_is_reachable_after_the_call(self):
        """调用方要能拿到 report 做进一步处理/打印。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = self._root(td)

            @guarded_main(str(repo.root))
            def probe() -> int:
                repo.write("src/util.py", "VALUE = 3\n")
                return 0

            probe()
            report = probe.last_report
            self.assertIsNotNone(report)
            self.assertIn("src/util.py", report.changed)

    def test_prints_a_summary_line(self):
        """探针日志里要能看到结论，否则等于没做。"""
        import io
        import tempfile
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as td:
            repo = self._root(td)

            @guarded_main(str(repo.root))
            def probe() -> int:
                return 0

            buf = io.StringIO()
            with redirect_stdout(buf):
                probe()
            self.assertIn("workspace guard", buf.getvalue())


class VerifyIdempotenceTest(unittest.TestCase):
    """verify() 重复调用必须返回同一份结论。

    真 bug（2026-11-14 自查发现）：docstring 写着「可重复调用」，实现却是
    每次都拿 `self._hashes`（原始快照）重算。第一次 verify 把文件还原了，
    第二次重算就发现「跟快照一致」→ 报告 clean，**污染记录被第二次调用抹掉**。

    这在 with 语句里就能触发：`__exit__` 调一次，调用方想打印又调一次，
    于是 `ok` 从 False 翻成 True，探针退出码变成 0。
    """

    def test_second_verify_keeps_the_contamination_record(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            guard = WorkspaceGuard(str(repo.root))
            guard.snapshot()
            repo.write("src/app.py", "print('dirty')\n")

            first = guard.verify()
            self.assertFalse(first.ok)

            second = guard.verify()
            self.assertEqual(
                first.changed, second.changed,
                "第二次 verify 把污染记录抹掉了 —— 探针会以 0 退出",
            )
            self.assertFalse(second.ok)

    def test_verify_after_context_exit_still_reports(self):
        """with 出来再 verify（很自然的用法）不能变成 clean。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            with WorkspaceGuard(str(repo.root)) as guard:
                repo.write("src/util.py", "VALUE = 7\n")
            self.assertFalse(guard.verify().ok)

    def test_restore_happens_only_once(self):
        """重复 verify 不该再 checkout 一次（那样会把用户之后的新改动也冲掉）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            guard = WorkspaceGuard(str(repo.root))
            guard.snapshot()
            repo.write("src/app.py", "print('dirty')\n")
            guard.verify()
            # 用户在探针之后又动手了
            repo.write("src/util.py", "VALUE = 42\n")
            guard.verify()
            self.assertEqual(
                "VALUE = 42\n", repo.read("src/util.py"),
                "第二次 verify 不该再还原，那会吃掉之后的新改动",
            )


class ScriptGuardTest(unittest.TestCase):
    """脚本式探针（无 main()，顶层直接跑）的护栏入口。

    仓库里很多探针是「顶层顺序执行」风格（`ga_tui_probe.py`、
    `ask_user_question_e2e_probe.py`），把它们整段缩进成 main() 会毁掉
    git blame。更小的侵入方式是两句：

        guard = start()          # 开头：快照
        ...
        finish(guard)            # 结尾：校验 + 定退出码（不返回则 sys.exit）

    `finish` 里自己 `sys.exit`，因为脚本式探针没有返回值可以带出去。
    """

    def test_start_and_finish_reverts_and_exits_nonzero(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            guard = start(str(repo.root))
            repo.write("src/app.py", "print('contaminated')\n")
            with self.assertRaises(SystemExit) as cm:
                finish(guard)
            self.assertEqual(CONTAMINATED_EXIT, cm.exception.code)
            self.assertEqual("print('hello')\n", repo.read("src/app.py"))

    def test_finish_returns_normally_when_clean(self):
        """干净时不 sys.exit —— 让脚本自然跑完。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            guard = start(str(repo.root))
            self.assertIsNone(finish(guard))

    def test_finish_can_be_given_an_explicit_code(self):
        """脚本自己有退出意图时能带出去。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            guard = start(str(repo.root))
            with self.assertRaises(SystemExit) as cm:
                finish(guard, code=3)
            self.assertEqual(3, cm.exception.code)

    def test_contamination_wins_over_explicit_code(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            guard = start(str(repo.root))
            repo.write("src/util.py", "VALUE = 0\n")
            with self.assertRaises(SystemExit) as cm:
                finish(guard, code=3)
            self.assertEqual(CONTAMINATED_EXIT, cm.exception.code)


if __name__ == "__main__":
    unittest.main()
