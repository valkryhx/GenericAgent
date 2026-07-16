"""llm_config / llm_client 单元测试。

覆盖：YAML 解析、pydantic 校验（extra=forbid、引用完整性、未知 capability）、
${ENV_VAR} 插值、密钥解析（明文优先 + env 备选）、profile 合并优先级
（defaults < model < profile）、wire_api → api_mode 映射、mixin chain 解析。
不依赖 llmcore（配置层可独立单测）。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from llm_config import (  # noqa: E402
    LLMConfig,
    load_llm_config,
    find_llm_config,
)
from pydantic import ValidationError  # noqa: E402


BASE_YAML = """
providers:
  anthropic:
    wire_api: anthropic
    base_url: https://api.anthropic.com
    api_key: sk-ant-plaintext
  openai:
    wire_api: openai_chat
    base_url: https://api.openai.com/v1
    api_key: sk-oai-plaintext
models:
  claude-opus-4-8:
    provider: anthropic
    context_window: 400000
    supports: [thinking, prompt_cache, 1m_context]
    thinking_type: adaptive
  gpt-5.4:
    provider: openai
    reasoning_effort: high
profiles:
  default:
    model: claude-opus-4-8
  fast:
    model: gpt-5.4
    reasoning_effort: minimal
    temperature: 0.3
defaults:
  max_tokens: 8192
  temperature: 1.0
  stream: true
active_profile: default
"""


def _write_yaml(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _parse(text: str) -> LLMConfig:
    return load_llm_config(_write_yaml(text))


class LoadAndValidateTest(unittest.TestCase):
    def test_parses_valid_config(self):
        cfg = _parse(BASE_YAML)
        self.assertEqual(set(cfg.providers), {"anthropic", "openai"})
        self.assertEqual(set(cfg.models), {"claude-opus-4-8", "gpt-5.4"})
        self.assertEqual(cfg.active_profile, "default")

    def test_unknown_top_level_field_rejected(self):
        with self.assertRaises(ValidationError):
            _parse(BASE_YAML + "\nbogus_field: 1\n")

    def test_unknown_provider_field_rejected(self):
        bad = BASE_YAML.replace(
            "    api_key: sk-ant-plaintext",
            "    api_key: sk-ant-plaintext\n    typo_field: x",
        )
        with self.assertRaises(ValidationError):
            _parse(bad)

    def test_model_referencing_missing_provider_rejected(self):
        bad = BASE_YAML.replace("    provider: openai", "    provider: nonexistent")
        with self.assertRaises(ValidationError):
            _parse(bad)

    def test_profile_referencing_missing_model_rejected(self):
        bad = BASE_YAML.replace("    model: gpt-5.4", "    model: ghost-model")
        with self.assertRaises(ValidationError):
            _parse(bad)

    def test_active_profile_must_exist(self):
        bad = BASE_YAML.replace("active_profile: default", "active_profile: nope")
        with self.assertRaises(ValidationError):
            _parse(bad)

    def test_unknown_capability_rejected(self):
        bad = BASE_YAML.replace(
            "supports: [thinking, prompt_cache, 1m_context]",
            "supports: [thinking, made_up_cap]",
        )
        with self.assertRaises(ValidationError):
            _parse(bad)

    def test_bad_wire_api_rejected(self):
        bad = BASE_YAML.replace("wire_api: openai_chat", "wire_api: openai_magic")
        with self.assertRaises(ValidationError):
            _parse(bad)


class KeyResolutionTest(unittest.TestCase):
    def test_plaintext_api_key_first_class(self):
        cfg = _parse(BASE_YAML)
        self.assertEqual(cfg.providers["anthropic"].resolve_api_key(), "sk-ant-plaintext")

    def test_env_key_fallback(self):
        os.environ["MY_TEST_LLM_KEY"] = "sk-from-env-123"
        try:
            text = """
providers:
  p:
    wire_api: openai_chat
    base_url: https://x/v1
    api_key_env: MY_TEST_LLM_KEY
models:
  m:
    provider: p
profiles:
  default:
    model: m
active_profile: default
"""
            cfg = _parse(text)
            self.assertEqual(cfg.providers["p"].resolve_api_key(), "sk-from-env-123")
        finally:
            del os.environ["MY_TEST_LLM_KEY"]

    def test_missing_env_key_raises(self):
        os.environ.pop("DEFINITELY_UNSET_KEY_XYZ", None)
        text = """
providers:
  p:
    wire_api: openai_chat
    base_url: https://x/v1
    api_key_env: DEFINITELY_UNSET_KEY_XYZ
models:
  m:
    provider: p
profiles:
  default:
    model: m
active_profile: default
"""
        cfg = _parse(text)
        with self.assertRaises(ValueError):
            cfg.providers["p"].resolve_api_key()

    def test_no_key_returns_empty_for_local(self):
        text = """
providers:
  local:
    wire_api: openai_chat
    base_url: http://localhost:8000/v1
models:
  m:
    provider: local
profiles:
  default:
    model: m
active_profile: default
"""
        cfg = _parse(text)
        self.assertEqual(cfg.providers["local"].resolve_api_key(), "")


class EnvInterpolationTest(unittest.TestCase):
    def test_env_interpolation_in_values(self):
        os.environ["GA_TEST_BASE"] = "https://interpolated.example/v1"
        os.environ["GA_TEST_KEY"] = "sk-interp"
        try:
            text = """
providers:
  p:
    wire_api: openai_chat
    base_url: ${GA_TEST_BASE}
    api_key: ${GA_TEST_KEY}
models:
  m:
    provider: p
profiles:
  default:
    model: m
active_profile: default
"""
            cfg = _parse(text)
            self.assertEqual(cfg.providers["p"].base_url, "https://interpolated.example/v1")
            self.assertEqual(cfg.providers["p"].resolve_api_key(), "sk-interp")
        finally:
            del os.environ["GA_TEST_BASE"]
            del os.environ["GA_TEST_KEY"]


class ResolveAndMergeTest(unittest.TestCase):
    def test_resolve_default_profile(self):
        cfg = _parse(BASE_YAML)
        r = cfg.resolve()
        self.assertEqual(r.name, "default")
        self.assertEqual(r.wire_api, "anthropic")
        self.assertEqual(r.model.provider, "anthropic")

    def test_profile_overrides_model_and_defaults(self):
        cfg = _parse(BASE_YAML)
        r = cfg.resolve("fast")
        # profile 覆盖 model 的 reasoning_effort（high → minimal），并新增 temperature=0.3
        self.assertEqual(r.params["reasoning_effort"], "minimal")
        self.assertEqual(r.params["temperature"], 0.3)
        # defaults 提供 max_tokens
        self.assertEqual(r.params["max_tokens"], 8192)

    def test_model_value_overrides_defaults(self):
        cfg = _parse(BASE_YAML)
        r = cfg.resolve("default")
        # model 声明 thinking_type=adaptive，defaults 没有 → 生效
        self.assertEqual(r.params["thinking_type"], "adaptive")

    def test_supports_lookup(self):
        cfg = _parse(BASE_YAML)
        r = cfg.resolve("default")
        self.assertTrue(r.supports("thinking"))
        self.assertTrue(r.supports("1m_context"))
        self.assertFalse(r.supports("image_input"))


class LegacyCfgTest(unittest.TestCase):
    def test_anthropic_cfg_shape(self):
        cfg = _parse(BASE_YAML)
        legacy = cfg.resolve("default").to_legacy_cfg()
        self.assertEqual(legacy["apikey"], "sk-ant-plaintext")
        self.assertEqual(legacy["apibase"], "https://api.anthropic.com")
        self.assertEqual(legacy["model"], "claude-opus-4-8")
        self.assertEqual(legacy["thinking_type"], "adaptive")
        self.assertEqual(legacy["context_win"], 400000)
        # anthropic wire 不写 api_mode
        self.assertNotIn("api_mode", legacy)

    def test_openai_chat_maps_to_chat_completions(self):
        cfg = _parse(BASE_YAML)
        legacy = cfg.resolve("fast").to_legacy_cfg()
        self.assertEqual(legacy["api_mode"], "chat_completions")
        self.assertEqual(legacy["apikey"], "sk-oai-plaintext")
        self.assertEqual(legacy["reasoning_effort"], "minimal")

    def test_openai_responses_maps_to_responses(self):
        text = BASE_YAML.replace(
            "    wire_api: openai_chat", "    wire_api: openai_responses"
        )
        cfg = _parse(text)
        legacy = cfg.resolve("fast").to_legacy_cfg()
        self.assertEqual(legacy["api_mode"], "responses")

    def test_api_model_overrides_key(self):
        text = BASE_YAML.replace(
            "  gpt-5.4:\n    provider: openai",
            "  gpt-5.4:\n    provider: openai\n    api_model: gpt-5.4-2026-01-01",
        )
        cfg = _parse(text)
        legacy = cfg.resolve("fast").to_legacy_cfg()
        self.assertEqual(legacy["model"], "gpt-5.4-2026-01-01")

    def test_channel_quirk_fields_carried(self):
        text = """
providers:
  relay:
    wire_api: anthropic
    base_url: http://host:2001
    api_key: relay-key
    fake_cc_system_prompt: true
    user_agent: "claude-cli/2.1.113 (external, cli)"
    extra_headers:
      X-Foo: bar
models:
  m:
    provider: relay
profiles:
  default:
    model: m
active_profile: default
"""
        cfg = _parse(text)
        legacy = cfg.resolve("default").to_legacy_cfg()
        self.assertTrue(legacy["fake_cc_system_prompt"])
        self.assertEqual(legacy["user_agent"], "claude-cli/2.1.113 (external, cli)")
        self.assertEqual(legacy["extra_headers"], {"X-Foo": "bar"})

    def test_image_input_capability_drives_native_image_input(self):
        # native_image_input 是「模型能力」，由 model.supports 里的 image_input
        # 推导，而不是 provider 字段。声明了就 True，没声明就 False。
        text = """
providers:
  openai:
    wire_api: openai_chat
    base_url: https://api.openai.com/v1
    api_key: sk-oai
models:
  with-img:
    provider: openai
    supports: [image_input]
  no-img:
    provider: openai
profiles:
  a:
    model: with-img
  b:
    model: no-img
active_profile: a
"""
        cfg = _parse(text)
        self.assertTrue(cfg.resolve("a").to_legacy_cfg()["native_image_input"])
        self.assertFalse(cfg.resolve("b").to_legacy_cfg()["native_image_input"])


class MixinAndResolveHelpersTest(unittest.TestCase):
    def test_mixin_chain_validated(self):
        text = BASE_YAML + """
mixin:
  failover:
    chain: [claude-opus-4-8, gpt-5.4]
    max_retries: 5
"""
        cfg = _parse(text)
        self.assertEqual(cfg.mixin["failover"].chain, ["claude-opus-4-8", "gpt-5.4"])

    def test_mixin_chain_missing_model_rejected(self):
        text = BASE_YAML + """
mixin:
  failover:
    chain: [claude-opus-4-8, ghost]
"""
        with self.assertRaises(ValidationError):
            _parse(text)

    def test_resolve_without_active_uses_single_profile(self):
        text = """
providers:
  p:
    wire_api: openai_chat
    base_url: https://x/v1
    api_key: k
models:
  m:
    provider: p
profiles:
  only:
    model: m
"""
        cfg = _parse(text)
        r = cfg.resolve()
        self.assertEqual(r.name, "only")

    def test_resolve_ambiguous_without_active_raises(self):
        text = BASE_YAML.replace("active_profile: default\n", "")
        cfg = _parse(text)
        with self.assertRaises(ValueError):
            cfg.resolve()


class FindConfigTest(unittest.TestCase):
    def test_find_llm_config_locates_yaml(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "llm.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(BASE_YAML)
        self.assertEqual(find_llm_config(d), p)

    def test_find_llm_config_none_when_absent(self):
        d = tempfile.mkdtemp()
        self.assertIsNone(find_llm_config(d))


if __name__ == "__main__":
    unittest.main()
