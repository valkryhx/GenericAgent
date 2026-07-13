from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


CONFIG_NAME = os.environ.get("GA_REAL_API_CONFIG", "native_oai_config")
EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "gpt-5.5")
EXPECTED_NAME = os.environ.get("GA_REAL_API_EXPECTED_NAME", "gpt-native")
OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"

SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._~+/=-]{24,})", re.IGNORECASE)


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if not isinstance(value, str):
        return value
    return SECRET_RE.sub("[REDACTED_SECRET]", value)


def parse_json_object(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def check_profile(summary: dict) -> bool:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        from llmcore import reload_mykeys

        cfg = reload_mykeys()[0].get(CONFIG_NAME) or {}
    profile = {
        "configName": CONFIG_NAME,
        "name": cfg.get("name"),
        "model": cfg.get("model"),
        "apiMode": cfg.get("api_mode", "chat_completions"),
        "loadLogChars": len(captured.getvalue()),
    }
    summary["profile"] = profile
    summary["profileOk"] = profile["model"] == EXPECTED_MODEL and (not EXPECTED_NAME or profile["name"] == EXPECTED_NAME)
    return summary["profileOk"]


def role_prompt(task_dir: str | None) -> str:
    import agentmain

    agent = type("RolePromptAgent", (), {"task_dir": task_dir})()
    return agentmain.get_system_prompt(agent)


def ask_real_model(system_prompt: str) -> tuple[str, dict]:
    from llmcore import resolve_client

    client = resolve_client(CONFIG_NAME)
    backend = getattr(client, "backend", None)
    if backend is not None:
        try:
            backend.max_tokens = 300
        except Exception:
            pass
    user_prompt = (
        "Read only your system instructions and output exactly one JSON object, no Markdown. "
        "Report which GA usage hint marker you see. Schema: "
        '{"role":"root|subagent","saw_root_marker":true|false,'
        '"saw_subagent_marker":true|false,"must_use_wait_agent_sparingly":true|false,'
        '"must_satisfy_final_answer_contract":true|false}.'
    )
    raw = "".join(
        client.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=None,
        )
    )
    return raw, parse_json_object(raw)


def main() -> int:
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "configName": CONFIG_NAME,
        "expectedName": EXPECTED_NAME,
        "expectedModel": EXPECTED_MODEL,
        "issues": [],
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_E2E=1 to run real gpt-5.5 role prompt smoke"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    try:
        if not check_profile(summary):
            summary["issues"].append("profile_mismatch")
            print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
            return 2

        root_raw, root = ask_real_model(role_prompt(None))
        sub_raw, subagent = ask_real_model(role_prompt(str(REPO / "temp" / "real_prompt_smoke_subagent")))
        summary["root"] = sanitize(root)
        summary["subagent"] = sanitize(subagent)
        summary["rootRawPreview"] = sanitize(root_raw[:300])
        summary["subagentRawPreview"] = sanitize(sub_raw[:300])

        if root.get("role") != "root":
            summary["issues"].append("root_role_not_detected")
        if root.get("saw_root_marker") is not True or root.get("saw_subagent_marker") is not False:
            summary["issues"].append("root_marker_detection_failed")
        if root.get("must_use_wait_agent_sparingly") is not True:
            summary["issues"].append("root_wait_policy_not_detected")
        if subagent.get("role") != "subagent":
            summary["issues"].append("subagent_role_not_detected")
        if subagent.get("saw_subagent_marker") is not True or subagent.get("saw_root_marker") is not False:
            summary["issues"].append("subagent_marker_detection_failed")
        if subagent.get("must_satisfy_final_answer_contract") is not True:
            summary["issues"].append("subagent_final_contract_not_detected")

        serialized = json.dumps(summary, ensure_ascii=False)
        if SECRET_RE.search(serialized):
            summary["issues"].append("summary_contains_secret_pattern")
        summary["passed"] = not summary["issues"]
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0 if summary["passed"] else 2
    except Exception as exc:
        summary["error"] = sanitize(f"{type(exc).__name__}: {exc}")
        summary["issues"].append("exception")
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
