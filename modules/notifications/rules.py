"""محرك تقييم القواعد — condition_json مقابل payload."""
from __future__ import annotations

import json
from typing import Any


def evaluate_conditions(condition_json: str, payload: dict[str, Any]) -> bool:
    try:
        cond = json.loads(condition_json or "{}")
    except json.JSONDecodeError:
        cond = {}
    if not cond:
        return True
    for key, expected in cond.items():
        actual = payload.get(key)
        if key == "min_shortage" and actual is not None:
            if float(actual) < float(expected):
                return False
            continue
        if key == "shortage_amount":
            actual = payload.get("shortage_amount") or payload.get("shortage")
            if actual is not None and float(actual) < float(expected):
                return False
            continue
        if key == "min_points" and actual is not None:
            if float(actual) < float(expected):
                return False
            continue
        if expected is not None and str(actual) != str(expected):
            return False
    return True


def match_rules_for_event(
    rules: list,
    event_key: str,
    payload: dict[str, Any],
) -> list:
    """يُرجع القواعد النشطة المطابقة للحدث والشروط."""
    out = []
    for rule in rules:
        if rule.event_key != event_key or not rule.is_active:
            continue
        if not evaluate_conditions(rule.condition_json, payload):
            continue
        out.append(rule)
    return out
