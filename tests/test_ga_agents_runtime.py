import tempfile
import unittest
from pathlib import Path


from ga_agents_runtime import (
    DEFAULT_GA_AGENTS_FILENAME,
    LOCAL_GA_AGENTS_FILENAME,
    build_ga_project_instructions,
    discover_ga_agents_paths,
    load_ga_project_instructions,
)


class GaAgentsRuntimeTest(unittest.TestCase):
    def test_discovers_docs_from_workspace_root_to_current_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            child = root / "frontends" / "ink-ui"
            child.mkdir(parents=True)
            (root / DEFAULT_GA_AGENTS_FILENAME).write_text("root rules", encoding="utf-8")
            (root / "frontends" / DEFAULT_GA_AGENTS_FILENAME).write_text("frontend rules", encoding="utf-8")
            (child / DEFAULT_GA_AGENTS_FILENAME).write_text("ink rules", encoding="utf-8")

            paths = discover_ga_agents_paths(root, child)

            self.assertEqual(
                [
                    root / DEFAULT_GA_AGENTS_FILENAME,
                    root / "frontends" / DEFAULT_GA_AGENTS_FILENAME,
                    child / DEFAULT_GA_AGENTS_FILENAME,
                ],
                paths,
            )

    def test_override_replaces_default_file_only_in_same_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            child = root / "pkg"
            child.mkdir()
            (root / DEFAULT_GA_AGENTS_FILENAME).write_text("root default", encoding="utf-8")
            (root / LOCAL_GA_AGENTS_FILENAME).write_text("root override", encoding="utf-8")
            (child / DEFAULT_GA_AGENTS_FILENAME).write_text("child default", encoding="utf-8")
            (child / LOCAL_GA_AGENTS_FILENAME).write_text("child override", encoding="utf-8")

            loaded = load_ga_project_instructions(root, child)

            self.assertEqual(
                [
                    str(Path(LOCAL_GA_AGENTS_FILENAME)),
                    str(Path("pkg") / LOCAL_GA_AGENTS_FILENAME),
                ],
                [doc.rel_path for doc in loaded.docs],
            )
            rendered = build_ga_project_instructions(root, child)
            self.assertIn("root override", rendered)
            self.assertIn("child override", rendered)
            self.assertNotIn("root default", rendered)
            self.assertNotIn("child default", rendered)

    def test_zero_budget_disables_project_instructions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / DEFAULT_GA_AGENTS_FILENAME).write_text("root rules", encoding="utf-8")

            loaded = load_ga_project_instructions(root, root, max_bytes=0)

            self.assertEqual((), loaded.docs)
            self.assertEqual("", build_ga_project_instructions(root, root, max_bytes=0))

    def test_budget_truncates_total_loaded_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            child = root / "pkg"
            child.mkdir()
            (root / DEFAULT_GA_AGENTS_FILENAME).write_text("abcdef", encoding="utf-8")
            (child / DEFAULT_GA_AGENTS_FILENAME).write_text("child rules", encoding="utf-8")

            loaded = load_ga_project_instructions(root, child, max_bytes=4)

            self.assertEqual(1, len(loaded.docs))
            self.assertEqual("abcd", loaded.docs[0].content)
            self.assertTrue(loaded.docs[0].truncated)
            self.assertTrue(loaded.truncated)

    def test_rendered_block_keeps_sources_and_root_to_cwd_order(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            child = root / "pkg"
            child.mkdir()
            (root / DEFAULT_GA_AGENTS_FILENAME).write_text("root rules", encoding="utf-8")
            (child / DEFAULT_GA_AGENTS_FILENAME).write_text("child rules", encoding="utf-8")

            rendered = build_ga_project_instructions(root, child)

            self.assertIn("[GA_PROJECT_INSTRUCTIONS]", rendered)
            self.assertIn(f"Source: {DEFAULT_GA_AGENTS_FILENAME}", rendered)
            self.assertIn(f"Source: {Path('pkg') / DEFAULT_GA_AGENTS_FILENAME}", rendered)
            self.assertLess(rendered.index("root rules"), rendered.index("child rules"))
            self.assertIn("later and more specific source", rendered)

    def test_current_dir_outside_workspace_falls_back_to_workspace_root(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as other:
            root = Path(td)
            outside = Path(other)
            (root / DEFAULT_GA_AGENTS_FILENAME).write_text("root rules", encoding="utf-8")
            (outside / DEFAULT_GA_AGENTS_FILENAME).write_text("outside rules", encoding="utf-8")

            rendered = build_ga_project_instructions(root, outside)

            self.assertIn("root rules", rendered)
            self.assertNotIn("outside rules", rendered)


class RepoPromptLayersTest(unittest.TestCase):
    """GA splits its prompt the way Codex does: base prompt vs project doc.

    `assets/sys_prompt.txt` carries identity + general capability guidance and
    applies to every workspace; `GA_AGENTS.md` carries this workspace's project
    knowledge. These assertions pin the split so guidance cannot silently drift
    out of both layers again.
    """

    def setUp(self):
        root = Path(__file__).resolve().parent.parent
        self.base = (root / "assets" / "sys_prompt.txt").read_text(encoding="utf-8")
        self.base_en = (root / "assets" / "sys_prompt_en.txt").read_text(encoding="utf-8")
        self.doc = (root / DEFAULT_GA_AGENTS_FILENAME).read_text(encoding="utf-8")

    def test_base_prompt_carries_identity_and_general_guidance(self):
        for heading in (
            "# 身份",
            "# 能力与边界",
            "# 怎么工作",
            "# 验证纪律",
            "# 工具使用",
            "# 沟通与交付",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, self.base)

    def test_base_prompt_pins_the_hard_lessons(self):
        for expected in (
            "不要用 code_run 手写 HTTP 抓取",
            "read_agent_result",
            "update_working_checkpoint",
            "没跑成或跑不动的验证要在汇报里明说",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, self.base)

    def test_english_base_prompt_stays_in_sync(self):
        for heading in ("# Identity", "# How you work", "# Verification discipline", "# Tool usage"):
            with self.subTest(heading=heading):
                self.assertIn(heading, self.base_en)
        self.assertIn("read_agent_result", self.base_en)

    def test_project_doc_stays_the_project_layer(self):
        for heading in (
            "## 路径与工作区",
            "## 子代理与 workflow",
            "## 工具速查（GA 实现细节）",
            "## 测试",
            "## 安全",
            "## 本仓库项目地图（GenericAgent）",
            "## GA_AGENTS.md 分层语义",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, self.doc)
        # The general working-style section belongs to the base prompt now.
        self.assertNotIn("## 怎么工作", self.doc)
        self.assertNotIn("## 检索与信息获取", self.doc)

    def test_project_doc_fits_the_default_injection_budget(self):
        from ga_agents_runtime import DEFAULT_PROJECT_DOC_MAX_BYTES

        self.assertLessEqual(len(self.doc.encode("utf-8")), DEFAULT_PROJECT_DOC_MAX_BYTES)

    def test_project_doc_does_not_claim_the_external_agents_md_name(self):
        # The file must stay GA-only; AGENTS.md is Codex's repo guidance and is
        # never read by the runtime.
        self.assertIn("避免与给外部 agent", self.doc)

    def test_workflow_child_prompt_includes_both_layers(self):
        from workflow_child_agent import NativeGPTChildAgentRunner

        prompt = NativeGPTChildAgentRunner()._build_system_prompt()
        self.assertIn("You are a workflow child agent", prompt)
        self.assertIn("# 能力与边界", prompt)
        self.assertIn("GA_PROJECT_INSTRUCTIONS", prompt)


if __name__ == "__main__":
    unittest.main()
