from __future__ import annotations

import copy


VERIFICATION_LEVELS = frozenset({"none", "inline", "full"})
CHECK_KINDS = frozenset({"command", "schema", "review", "diff", "manual", "artifact"})
CHECK_OWNERS = frozenset({"host", "implementation", "independent_agent"})


def _text(value):
    return str(value or "").strip()


def _require_level(level):
    level = _text(level).lower() or "none"
    if level not in VERIFICATION_LEVELS:
        raise ValueError(f"unsupported verification level: {level}")
    return level


def _normalize_check(raw, index):
    if isinstance(raw, str):
        raw = {"id": raw, "kind": raw, "required": True}
    if not isinstance(raw, dict):
        raise ValueError(f"verification check {index} must be an object")
    check = copy.deepcopy(raw)
    check_id = _text(check.get("id"))
    kind = _text(check.get("kind") or check.get("type")).lower()
    if kind == "verification_schema":
        kind = "schema"
    if not check_id:
        raise ValueError(f"verification check {index} requires id")
    if not kind:
        raise ValueError(f"verification check {check_id} requires kind")
    if kind not in CHECK_KINDS:
        raise ValueError(f"unsupported verification check kind: {kind}")
    required = check.get("required", True)
    if not isinstance(required, bool):
        raise ValueError(f"verification check {check_id} required must be boolean")
    owner = _text(check.get("owner"))
    if not owner:
        owner = "implementation" if kind == "schema" else ("independent_agent" if kind == "review" else "host")
    if owner not in CHECK_OWNERS:
        raise ValueError(f"unsupported verification check owner: {owner}")
    check["id"] = check_id
    check["kind"] = kind
    check["required"] = required
    check["owner"] = owner
    if kind == "schema" and not _text(check.get("schemaRef")):
        raise ValueError(f"verification schema check {check_id} requires schemaRef")
    if kind == "command" and not (check.get("command") or check.get("adapter")):
        raise ValueError(f"verification command check {check_id} requires command or adapter")
    return check


def normalize_checks(raw_checks):
    if raw_checks is None:
        raw_checks = []
    if not isinstance(raw_checks, list):
        raise ValueError("verification checks must be a list")
    checks = [_normalize_check(item, index) for index, item in enumerate(raw_checks)]
    ids = [check["id"] for check in checks]
    if len(ids) != len(set(ids)):
        raise ValueError("verification check ids must be unique")
    return checks


def validate_verification_contract(contract):
    if not isinstance(contract, dict):
        raise ValueError("verification contract must be an object")
    level = _require_level(contract.get("level"))
    checks = normalize_checks(contract.get("checks") or [])
    independent = contract.get("independentReview", False)
    if not isinstance(independent, bool):
        raise ValueError("verification independentReview must be boolean")
    if level == "none" and any(check.get("required") for check in checks):
        raise ValueError("verification level none cannot contain required checks")
    normalized = copy.deepcopy(contract)
    normalized.update({"level": level, "checks": checks, "independentReview": independent})
    return normalized


def _legacy_contract(plan):
    acceptance = plan.get("acceptance") if isinstance(plan, dict) else None
    raw_checks = acceptance.get("checks") if isinstance(acceptance, dict) else []
    checks = []
    for item in raw_checks or []:
        name = _text(item.get("type") if isinstance(item, dict) else item).lower()
        if name == "python_unittest":
            checks.append(
                {
                    "id": "legacy-python-unittest",
                    "kind": "command",
                    "adapter": "python_unittest",
                    "required": True,
                    "owner": "host",
                }
            )
        elif name in {"verification", "verification_schema"}:
            checks.append(
                {
                    "id": "legacy-verification-schema",
                    "kind": "schema",
                    "schemaRef": "verification_result",
                    "required": True,
                    "owner": "implementation",
                }
            )
    agents = plan.get("agents") if isinstance(plan, dict) else []
    independent = any(_text(agent.get("role")).lower() == "verification" for agent in agents or [] if isinstance(agent, dict))
    return {
        "level": "full" if independent else ("inline" if checks else "none"),
        "checks": checks,
        "independentReview": independent,
        "metadata": {"legacyConverted": True},
    }


def normalize_verification_contract(plan):
    if not isinstance(plan, dict):
        raise ValueError("workflow plan must be an object")
    raw = plan.get("verification")
    if isinstance(raw, dict):
        contract = {
            "level": raw.get("level") or "inline",
            "checks": raw.get("checks") or [],
            "independentReview": raw.get("independentReview", False),
            "metadata": {"legacyConverted": False},
        }
        contract.update({key: copy.deepcopy(value) for key, value in raw.items() if key not in contract})
        return validate_verification_contract(contract)
    return validate_verification_contract(_legacy_contract(plan))


def required_checks(contract):
    normalized = validate_verification_contract(contract)
    return [check for check in normalized["checks"] if check["required"]]
