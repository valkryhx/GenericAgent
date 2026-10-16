"""Workflow LLM binding — align child/planner with main-session llm.yaml profiles.

Production path: profile from GenericAgent current /model (backend.name),
then build a *fresh* NativeToolClient via llm_client.build_client so child
history stays isolated from the parent session.

Legacy mykey resolve_session(config_name) is not used as the default path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class WorkflowLlmBinding:
    profile_name: str
    model_id: str = ""
    source: str = "profile"  # agent | profile | env | explicit

    def as_metadata(self) -> dict[str, str]:
        return {
            "llmProfile": self.profile_name or "",
            "llmModel": self.model_id or "",
            "llmSource": self.source or "",
        }


def _load_config(config_path: str | None = None):
    from llm_config import find_llm_config, load_llm_config

    path = config_path or find_llm_config()
    if not path:
        raise FileNotFoundError(
            "未找到 llm.yaml / llm.yml。workflow child/planner 已对齐主会话 YAML 配置，"
            "请在仓库根目录放置 llm.yaml。"
        )
    return load_llm_config(path), path


def binding_from_agent(agent: Any) -> WorkflowLlmBinding:
    """Snapshot current main-session /model selection (profile + model id)."""
    client = getattr(agent, "llmclient", None)
    backend = getattr(client, "backend", None) if client is not None else None
    profile = getattr(backend, "name", None) if backend is not None else None
    model = getattr(backend, "model", None) if backend is not None else None
    profile_name = str(profile or "").strip()
    model_id = str(model or "").strip()
    if not profile_name:
        # Fall back to yaml active_profile when agent has no client yet.
        return binding_from_env()
    return WorkflowLlmBinding(profile_name=profile_name, model_id=model_id, source="agent")


def binding_from_profile(profile_name: str, *, config=None, config_path: str | None = None) -> WorkflowLlmBinding:
    name = str(profile_name or "").strip()
    if not name:
        raise ValueError("workflow llm profile_name is required")
    if config is None:
        config, _ = _load_config(config_path)
    if name not in config.profiles:
        available = ", ".join(sorted(config.profiles)) or "(none)"
        raise ValueError(f"llm.yaml 中不存在 profile '{name}'；可用: {available}")
    resolved = config.resolve(name)
    model_id = ""
    if hasattr(resolved, "to_legacy_cfg"):
        try:
            model_id = str((resolved.to_legacy_cfg() or {}).get("model") or "")
        except Exception:
            model_id = ""
    if not model_id:
        # ProfileCfg.model is the model *key* in yaml (e.g. grok-4.5)
        try:
            model_id = str(config.profiles[name].model or "")
        except Exception:
            model_id = ""
    return WorkflowLlmBinding(profile_name=name, model_id=model_id, source="profile")


def _legacy_mykey_key(name: str) -> bool:
    n = (name or "").strip().lower()
    return n.endswith("_config") or n.startswith("native_") or "mykey" in n


def binding_from_env(*, config_path: str | None = None) -> WorkflowLlmBinding:
    """Resolve binding from env profile name or llm.yaml active_profile."""
    explicit = (
        os.environ.get("GA_WORKFLOW_LLM_PROFILE")
        or os.environ.get("GA_REAL_API_PROFILE")
        or os.environ.get("GA_WORKFLOW_PLANNER_CONFIG")
        or os.environ.get("GA_REAL_API_CONFIG")
        or ""
    ).strip()
    config, _ = _load_config(config_path)
    if explicit and not _legacy_mykey_key(explicit):
        return binding_from_profile(explicit, config=config)
    # Ignore legacy mykey keys like native_oai_config — use active_profile.
    active = str(getattr(config, "active_profile", None) or "").strip()
    if not active:
        # first profile declaration order
        if config.profiles:
            active = next(iter(config.profiles))
        else:
            raise ValueError("llm.yaml 未配置 active_profile 且 profiles 为空")
    binding = binding_from_profile(active, config=config)
    return WorkflowLlmBinding(
        profile_name=binding.profile_name,
        model_id=binding.model_id,
        source="env",
    )


def make_tool_client(binding: WorkflowLlmBinding, *, config_path: str | None = None):
    """Build a fresh NativeToolClient for this profile (isolated history)."""
    from llm_client import build_client

    config, _ = _load_config(config_path)
    return build_client(config, binding.profile_name)


def make_session(binding: WorkflowLlmBinding, *, config_path: str | None = None):
    """Build a fresh Session (no ToolClient wrap) — planner-friendly."""
    from llm_client import build_session

    config, _ = _load_config(config_path)
    return build_session(config, binding.profile_name)


def resolve_binding(
    *,
    agent: Any = None,
    profile_name: str | None = None,
    binding_provider: Callable[[], WorkflowLlmBinding] | None = None,
) -> WorkflowLlmBinding:
    if binding_provider is not None:
        binding = binding_provider()
        if not isinstance(binding, WorkflowLlmBinding):
            raise TypeError("binding_provider must return WorkflowLlmBinding")
        return binding
    if profile_name:
        return binding_from_profile(profile_name)
    if agent is not None:
        return binding_from_agent(agent)
    return binding_from_env()
