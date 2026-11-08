"""What tool set does a subagent ACTUALLY get, under each spawn mode?

Reported symptom from a real GA run: four subagents were spawned to research, and
all four came back saying their tool set was only task_* / send_message /
team_status -- no web_search, no code_run -- so they could not do the job and
reported blocked. The parent had passed no explicit ``tools`` list.

The suspicion is that a spawn mode which is supposed to *add* collaboration tools
ends up *subtracting* the ordinary ones. This calls the real
build_subagent_capability_profile with the real schema and reports what survives,
so the answer is measured rather than read off the code.
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from genericagent.agents.capabilities import (  # noqa: E402
    ORCHESTRATION_TOOLS,
    TEAMMATE_TOOLS,
    build_subagent_capability_profile,
)

SCHEMA = REPO / "assets" / "tools_schema.json"


def tool_names():
    data = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return [item["function"]["name"] for item in data if isinstance(item, dict)]


def report(label, **kwargs):
    available = tool_names()
    profile = build_subagent_capability_profile(available_tools=available, **kwargs)
    allowed = sorted(profile.allowed_tools)
    ordinary = [n for n in allowed if n not in ORCHESTRATION_TOOLS and n not in TEAMMATE_TOOLS]
    print(f"\n=== {label} ===")
    print(f"  schema tools        : {len(available)}")
    print(f"  allowed             : {len(allowed)}")
    print(f"  ordinary (non-agent): {len(ordinary)}")
    print(f"  first 12 ordinary   : {ordinary[:12]}")
    print(f"  has web_search_exa  : {'web_search_exa' in allowed}")
    print(f"  has code_run        : {'code_run' in allowed}")
    print(f"  has file_read       : {'file_read' in allowed}")
    print(f"  has spawn_agent     : {'spawn_agent' in allowed}")
    return allowed


def main():
    available = tool_names()
    print("available in schema:", len(available))
    print("web_search-ish:", [n for n in available if "search" in n.lower() or "web" in n.lower()][:8])

    report("generic subagent (no role_tools, no team_mode)", is_subagent=True)
    report("subagent team_mode=True", is_subagent=True, team_mode=True)
    report("subagent with role_tools=['file_read','code_run']", is_subagent=True, role_tools=["file_read", "code_run"])
    report("ROLE WITH EMPTY tools list (role.tools == ())", is_subagent=True, role_tools=[])
    report("subagent allow_delegation=True", is_subagent=True, allow_delegation=True)
    report("main agent", is_subagent=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
