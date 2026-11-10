"""摘要 prompt 的可本地化覆盖（环境变量指向模板文件）。

默认仍是英文（与 CC 一致）；需要中文化时把模板文件指给 GA_COMPACT_PROMPT_FILE，
支持 {{SECTIONS}} / {{GUARDS}} / {{SCOPE}} / {{INSTRUCTIONS}} / {{SOURCE}} 占位符。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from genericagent.core import context as compact_context  # noqa: E402


class CompactPromptFileTest(unittest.TestCase):
    def test_default_prompt_is_unchanged_without_override(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(compact_context.COMPACT_PROMPT_FILE_ENV, None)
            prompt = compact_context._compact_prompt("SRC", "keep paths")
        self.assertIn("CRITICAL: Respond with TEXT ONLY", prompt)
        self.assertIn("Conversation to compact:", prompt)
        self.assertIn("SRC", prompt)
        self.assertIn("keep paths", prompt)

    def test_template_file_replaces_the_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp, "compact_zh.md")
            template.write_text(
                "只输出中文摘要，不要调用工具。\n\n章节：\n{{SECTIONS}}\n\n"
                "用户要求：{{INSTRUCTIONS}}\n\n待压缩对话：\n{{SOURCE}}\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {compact_context.COMPACT_PROMPT_FILE_ENV: str(template)}):
                prompt = compact_context._compact_prompt("SRC-BODY", "保留路径")
        self.assertIn("只输出中文摘要", prompt)
        self.assertNotIn("CRITICAL: Respond with TEXT ONLY", prompt)
        self.assertIn("SRC-BODY", prompt)
        self.assertIn("保留路径", prompt)
        self.assertIn("Primary Request and Intent", prompt)

    def test_template_without_source_placeholder_still_carries_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp, "compact_zh.md")
            template.write_text("请总结下面的对话。\n", encoding="utf-8")
            with patch.dict(os.environ, {compact_context.COMPACT_PROMPT_FILE_ENV: str(template)}):
                prompt = compact_context._compact_prompt("SRC-BODY", "")
        self.assertTrue(prompt.startswith("请总结下面的对话。"))
        self.assertIn("SRC-BODY", prompt)

    def test_missing_template_file_falls_back_to_default(self):
        with patch.dict(
            os.environ,
            {compact_context.COMPACT_PROMPT_FILE_ENV: str(Path(tempfile.gettempdir(), "nope-missing.md"))},
        ):
            prompt = compact_context._compact_prompt("SRC", "")
        self.assertIn("CRITICAL: Respond with TEXT ONLY", prompt)

    def test_direction_scope_is_available_to_the_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp, "compact_zh.md")
            template.write_text("范围：{{SCOPE}}\n对话：\n{{SOURCE}}", encoding="utf-8")
            with patch.dict(os.environ, {compact_context.COMPACT_PROMPT_FILE_ENV: str(template)}):
                prompt = compact_context._compact_prompt("SRC", "", direction="from")
        self.assertIn("kept verbatim", prompt)


if __name__ == "__main__":
    unittest.main()
