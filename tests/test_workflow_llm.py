import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from workflow_llm import (
    WorkflowLlmBinding,
    binding_from_agent,
    binding_from_env,
    binding_from_profile,
    resolve_binding,
)


MINI_YAML = """
providers:
  p1:
    wire_api: openai_chat
    base_url: http://example.invalid/v1
    api_key_env: FAKE_KEY_FOR_TEST
models:
  m-fast:
    provider: p1
    context_window: 8000
  m-slow:
    provider: p1
    context_window: 8000
profiles:
  alpha:
    model: m-fast
  beta:
    model: m-slow
defaults:
  max_tokens: 16
active_profile: alpha
"""


class WorkflowLlmBindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.yaml_path = Path(self.tmp.name) / "llm.yaml"
        self.yaml_path.write_text(MINI_YAML, encoding="utf-8")
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        for key in (
            "GA_WORKFLOW_LLM_PROFILE",
            "GA_REAL_API_PROFILE",
            "GA_WORKFLOW_PLANNER_CONFIG",
            "GA_REAL_API_CONFIG",
        ):
            os.environ.pop(key, None)

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_binding_from_profile_reads_model_id(self):
        binding = binding_from_profile("beta", config_path=str(self.yaml_path))
        self.assertEqual("beta", binding.profile_name)
        self.assertEqual("m-slow", binding.model_id)
        self.assertEqual("profile", binding.source)
        meta = binding.as_metadata()
        self.assertEqual("beta", meta["llmProfile"])
        self.assertEqual("m-slow", meta["llmModel"])

    def test_unknown_profile_raises(self):
        with self.assertRaises(ValueError) as ctx:
            binding_from_profile("nope", config_path=str(self.yaml_path))
        self.assertIn("nope", str(ctx.exception))

    def test_binding_from_agent_uses_backend_name_and_model(self):
        agent = SimpleNamespace(
            llmclient=SimpleNamespace(backend=SimpleNamespace(name="grok", model="grok-4.5"))
        )
        binding = binding_from_agent(agent)
        self.assertEqual("grok", binding.profile_name)
        self.assertEqual("grok-4.5", binding.model_id)
        self.assertEqual("agent", binding.source)

    def test_binding_from_env_uses_active_profile(self):
        binding = binding_from_env(config_path=str(self.yaml_path))
        self.assertEqual("alpha", binding.profile_name)
        self.assertEqual("m-fast", binding.model_id)

    def test_binding_from_env_respects_profile_env(self):
        os.environ["GA_WORKFLOW_LLM_PROFILE"] = "beta"
        binding = binding_from_env(config_path=str(self.yaml_path))
        self.assertEqual("beta", binding.profile_name)
        self.assertEqual("m-slow", binding.model_id)

    def test_binding_from_env_ignores_legacy_mykey_config_name(self):
        os.environ["GA_REAL_API_CONFIG"] = "native_oai_config"
        binding = binding_from_env(config_path=str(self.yaml_path))
        self.assertEqual("alpha", binding.profile_name)

    def test_resolve_binding_provider_wins(self):
        fixed = WorkflowLlmBinding(profile_name="beta", model_id="m-slow", source="explicit")
        got = resolve_binding(binding_provider=lambda: fixed, profile_name="alpha")
        self.assertEqual(fixed, got)

    def test_model_switch_snapshot_changes_with_agent(self):
        """Simulates /model switch: later jobs see new profile via binding_from_agent."""
        backend = SimpleNamespace(name="alpha", model="m-fast")
        agent = SimpleNamespace(llmclient=SimpleNamespace(backend=backend))
        first = binding_from_agent(agent)
        self.assertEqual("alpha", first.profile_name)
        backend.name = "beta"
        backend.model = "m-slow"
        second = binding_from_agent(agent)
        self.assertEqual("beta", second.profile_name)
        self.assertEqual("m-slow", second.model_id)


class WorkflowChildUsesBindingTest(unittest.TestCase):
    def test_native_runner_uses_binding_provider_not_mykey(self):
        from workflow_child_agent import NativeGPTChildAgentRunner

        calls = []

        class StubClient:
            def __init__(self):
                self.backend = SimpleNamespace(history=[], model="x", name="p")
                self.last_usage_tokens = {}

        def factory():
            calls.append("client")
            return StubClient()

        # When client_factory present, no yaml needed
        runner = NativeGPTChildAgentRunner(client_factory=lambda _k: factory())
        # _new_executable path
        exe, is_tool, meta = runner._new_executable()
        self.assertTrue(is_tool)
        self.assertEqual(1, len(calls))
        self.assertEqual("client_factory", meta.get("llmSource"))

    def test_native_runner_binding_provider_builds_meta(self):
        from workflow_child_agent import NativeGPTChildAgentRunner
        from workflow_llm import WorkflowLlmBinding

        binding = WorkflowLlmBinding(profile_name="grok", model_id="grok-4.5", source="agent")
        built = []

        def fake_make_tool_client(b, **kwargs):
            built.append(b)
            return SimpleNamespace(backend=SimpleNamespace(history=[], model=b.model_id, name=b.profile_name))

        with patch("workflow_llm.make_tool_client", side_effect=fake_make_tool_client):
            # patch where used inside child module path — child imports make_tool_client inside method
            with patch("workflow_child_agent.NativeGPTChildAgentRunner._new_executable") as mocked:
                # exercise real method by not mocking — patch workflow_llm.make_tool_client via import site
                pass

        with patch.dict("sys.modules", {}):
            pass

        # Directly patch symbols imported inside _new_executable
        import workflow_llm as wllm

        original = wllm.make_tool_client
        try:
            wllm.make_tool_client = fake_make_tool_client  # type: ignore
            runner = NativeGPTChildAgentRunner(
                binding_provider=lambda: binding,
                enable_tools=True,
            )
            # re-import path: method does `from workflow_llm import make_tool_client`
            # so patch on module attribute works for subsequent imports in CPython if from-import reloads from module
            exe, is_tool, meta = runner._new_executable()
            self.assertTrue(is_tool)
            self.assertEqual("grok", meta.get("llmProfile"))
            self.assertEqual("grok-4.5", meta.get("llmModel"))
            self.assertEqual("agent", meta.get("llmSource"))
            self.assertEqual(1, len(built))
            self.assertEqual("grok", runner.config_name)
        finally:
            wllm.make_tool_client = original  # type: ignore


class InkBridgeDefaultRunnerTest(unittest.TestCase):
    def test_make_workflow_runtime_uses_native_runner_bound_to_agent(self):
        from frontends.ink_bridge import GenericAgentBridge
        from workflow_child_agent import FakeChildAgentRunner, NativeGPTChildAgentRunner

        class Agent:
            is_running = False
            session_id = "s"
            permission_mode = "full_access"
            handler = None
            llmclient = SimpleNamespace(backend=SimpleNamespace(name="grok", model="grok-4.5"))

            def run(self):
                return None

        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: Agent(), emit=events.append)
        # Ensure agent property exists
        if not hasattr(bridge, "agent") or bridge.agent is None:
            bridge.agent = Agent()
        runtime = bridge._make_workflow_runtime(timeout_seconds=5)
        self.assertIsInstance(runtime.runner, NativeGPTChildAgentRunner)
        self.assertNotIsInstance(runtime.runner, FakeChildAgentRunner)
        self.assertTrue(callable(runtime.runner.binding_provider))
        binding = runtime.runner.binding_provider()
        self.assertEqual("grok", binding.profile_name)
        self.assertEqual("grok-4.5", binding.model_id)


if __name__ == "__main__":
    unittest.main()
