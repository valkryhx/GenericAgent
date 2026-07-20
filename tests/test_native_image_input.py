import base64
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agentmain import (  # noqa: E402
    _build_user_content_with_images,
    _native_image_input_enabled,
    supports_image_input,
    build_vision_direct_answer_sys_prompt,
    should_inject_vision_direct_answer,
    history_has_native_images,
    content_has_native_images,
    user_query_looks_like_vision_followup,
)
from agent_loop import agent_runner_loop  # noqa: E402
from llmcore import (  # noqa: E402
    NativeClaudeSession,
    NativeOAISession,
    NativeToolClient,
    _msgs_claude2oai,
    _redact_image_payloads,
    _to_responses_input,
)


# 合法 8x8 PNG（满足 image_codec.MIN_DIMENSION=8）
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGM8YWTEgA0wYRUdtBIA76YBPGvKGPUAAAAASUVORK5CYII="
)


class NativeImageInputTest(unittest.TestCase):
    def test_native_image_input_only_enabled_for_configured_native_oai(self):
        backend = NativeOAISession({
            "apikey": "sk-test",
            "apibase": "https://example.test/v1",
            "model": "gpt-5.5",
            "native_image_input": True,
        })

        self.assertTrue(_native_image_input_enabled(type("Client", (), {"backend": backend})()))
        self.assertTrue(supports_image_input(type("Client", (), {"backend": backend})()))

        mixin_backend = type("MixinBackend", (), {"primary": backend})()
        self.assertTrue(_native_image_input_enabled(type("Client", (), {"backend": mixin_backend})()))

        backend.native_image_input = False
        self.assertFalse(_native_image_input_enabled(type("Client", (), {"backend": backend})()))

        self.assertFalse(_native_image_input_enabled(type("Client", (), {"backend": object()})()))

    def test_native_claude_supports_image_input_by_default(self):
        backend = NativeClaudeSession({
            "apikey": "sk-ant-test",
            "apibase": "https://example.test",
            "model": "claude-sonnet-4",
        })
        self.assertTrue(supports_image_input(type("Client", (), {"backend": backend})()))

    def test_agentmain_builds_native_image_blocks_from_path(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(_TINY_PNG)
            path = Path(f.name)
        try:
            content = _build_user_content_with_images(f'"{path}" 这图内容是？')
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(content[0]["type"], "text")
        self.assertIn("这图内容是", content[0]["text"])
        self.assertEqual(content[1]["type"], "image")
        self.assertEqual(content[1]["source"]["type"], "base64")
        self.assertEqual(content[1]["source"]["media_type"], "image/png")
        self.assertEqual(base64.b64decode(content[1]["source"]["data"]), _TINY_PNG)

    def test_agentmain_builds_native_image_blocks_from_unquoted_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "screen shot 2026-05-21 165002.png"
            path.write_bytes(_TINY_PNG)

            content = _build_user_content_with_images(f"{path} 内容是？")

        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image")
        self.assertEqual(base64.b64decode(content[1]["source"]["data"]), _TINY_PNG)

    def test_agentmain_leaves_plain_text_on_original_path(self):
        self.assertIsNone(_build_user_content_with_images("你好"))

    def test_openai_converter_collapses_text_only_user_content(self):
        chat = _msgs_claude2oai([
            {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        ])

        self.assertEqual(chat[0]["content"], "hi")

    def test_openai_converters_preserve_native_image_blocks(self):
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "描述图片"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
            ],
        }

        chat = _msgs_claude2oai([msg])
        self.assertEqual(chat[0]["content"][1]["type"], "image_url")
        self.assertTrue(chat[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

        responses = _to_responses_input(chat)
        self.assertEqual(responses[0]["content"][1]["type"], "input_image")

        # Claude-style block 直接进 responses 转换也不丢
        responses2 = _to_responses_input([msg])
        self.assertTrue(
            any(
                isinstance(p, dict) and p.get("type") == "input_image"
                for p in responses2[0]["content"]
            )
        )

    def test_redact_image_payloads_strips_base64(self):
        payload = {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 500}},
            ],
        }
        red = _redact_image_payloads(payload)
        data = red["content"][1]["source"]["data"]
        self.assertIn("redacted", data)
        self.assertNotIn("A" * 50, data)

    def test_native_tool_client_keeps_non_text_content_blocks(self):
        class Backend:
            name = "fake"
            history = []
            system = ""
            tools = None

            def ask(self, merged):
                self.merged = merged
                if False:
                    yield ""
                return None

        backend = Backend()
        client = NativeToolClient(backend)
        list(
            client.chat(
                messages=[
                    {"role": "user", "content": [{"type": "text", "text": "看图"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}
                ]
            )
        )

        self.assertEqual(backend.merged["content"][1]["type"], "image_url")

    def test_native_tool_client_can_disable_native_tools(self):
        class Backend:
            name = "fake"
            history = []
            system = ""
            tools = "unset"
            native_tools = False

            def ask(self, merged):
                self.merged = merged
                if False:
                    yield ""
                return None

        backend = Backend()
        client = NativeToolClient(backend)
        tools = [{"type": "function", "function": {"name": "code_run", "parameters": {"type": "object", "properties": {}}}}]
        list(client.chat(messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], tools=tools))

        self.assertIsNone(backend.tools)
        self.assertIn("Tools (mounted", backend.system)
        self.assertEqual(backend.merged["content"][0]["text"], "hi")

    def test_agent_runner_uses_initial_multimodal_content(self):
        class Response:
            content = "ok"
            tool_calls = []

        class Client:
            last_tools = ""

            def chat(self, messages, tools=None):
                self.messages = messages
                if False:
                    yield ""
                return Response()

        class Handler:
            max_turns = 1
            _done_hooks = []
            parent = type("Parent", (), {"task_dir": None})()

            def dispatch(self, *args, **kwargs):
                if False:
                    yield None
                from agent_loop import StepOutcome

                return StepOutcome("done", next_prompt=None)

            def turn_end_callback(self, *args, **kwargs):
                return ""

        client = Client()
        content = [{"type": "text", "text": "看图"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
        list(agent_runner_loop(client, "sys", "fallback", Handler(), [], max_turns=1, verbose=False, initial_user_content=content))

        self.assertEqual(client.messages[1]["content"], content)


class VisionDirectAnswerPromptTest(unittest.TestCase):
    def test_content_has_native_images_detects_claude_and_oai(self):
        self.assertTrue(content_has_native_images([
            {"type": "text", "text": "x"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}},
        ]))
        self.assertTrue(content_has_native_images([
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]))
        self.assertFalse(content_has_native_images([{"type": "text", "text": "[Image #1]"}]))

    def test_history_has_native_images(self):
        hist = [
            {"role": "user", "content": [{"type": "text", "text": "a"}]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}},
                ],
            },
        ]
        self.assertTrue(history_has_native_images(hist))
        self.assertFalse(history_has_native_images([{"role": "user", "content": "hi"}]))

    def test_followup_query_detection(self):
        self.assertTrue(user_query_looks_like_vision_followup("重试"))
        self.assertTrue(user_query_looks_like_vision_followup("再描述一下"))
        self.assertTrue(user_query_looks_like_vision_followup("describe the image"))
        self.assertFalse(user_query_looks_like_vision_followup("请重构 agentmain.py 的 put_task"))

    def test_inject_on_current_images(self):
        content = [
            {"type": "text", "text": "[Image #1] 是什么"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "BB"}},
        ]
        inject, via, n = should_inject_vision_direct_answer(
            can_image=True, initial_content=content, history=[], user_text="[Image #1] 是什么"
        )
        self.assertTrue(inject)
        self.assertEqual(via, "current")
        self.assertEqual(n, 2)

    def test_inject_on_history_retry(self):
        hist = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "[Image #1]"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}},
            ],
        }]
        inject, via, n = should_inject_vision_direct_answer(
            can_image=True, initial_content=None, history=hist, user_text="重试"
        )
        self.assertTrue(inject)
        self.assertEqual(via, "history")
        self.assertEqual(n, 1)

    def test_no_inject_without_images_or_gate(self):
        inject, _, _ = should_inject_vision_direct_answer(
            can_image=False,
            initial_content=[{"type": "image", "source": {"type": "base64", "data": "AA"}}],
            history=[],
            user_text="看图",
        )
        self.assertFalse(inject)
        inject2, _, _ = should_inject_vision_direct_answer(
            can_image=True, initial_content=None, history=[], user_text="重试"
        )
        self.assertFalse(inject2)

    def test_prompt_text_bans_placeholder_code_search(self):
        p = build_vision_direct_answer_sys_prompt(image_count=5, via="current")
        self.assertIn("Native vision", p)
        self.assertIn("[Image #N]", p)
        self.assertIn("code_run", p)
        self.assertIn("OCR", p)
        self.assertIn("5", p)
        p2 = build_vision_direct_answer_sys_prompt(image_count=1, via="history")
        self.assertIn("history", p2)


if __name__ == "__main__":
    unittest.main()
