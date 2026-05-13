import base64
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agentmain import _build_user_content_with_images, _native_image_input_enabled  # noqa: E402
from agent_loop import agent_runner_loop  # noqa: E402
from llmcore import NativeOAISession, NativeToolClient, _msgs_claude2oai, _to_responses_input  # noqa: E402


class NativeImageInputTest(unittest.TestCase):
    def test_native_image_input_only_enabled_for_configured_native_oai(self):
        backend = NativeOAISession({
            "apikey": "sk-test",
            "apibase": "https://example.test/v1",
            "model": "gpt-5.5",
            "native_image_input": True,
        })

        self.assertTrue(_native_image_input_enabled(type("Client", (), {"backend": backend})()))

        mixin_backend = type("MixinBackend", (), {"primary": backend})()
        self.assertTrue(_native_image_input_enabled(type("Client", (), {"backend": mixin_backend})()))

        backend.native_image_input = False
        self.assertFalse(_native_image_input_enabled(type("Client", (), {"backend": backend})()))

        self.assertFalse(_native_image_input_enabled(type("Client", (), {"backend": object()})()))

    def test_agentmain_builds_native_image_blocks_from_path(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\nfake")
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
        self.assertEqual(base64.b64decode(content[1]["source"]["data"]), b"\x89PNG\r\n\x1a\nfake")

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


if __name__ == "__main__":
    unittest.main()
