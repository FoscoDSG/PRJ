"""Score the rules baseline against labeled cases. Human overrides are a separate table."""

from __future__ import annotations

import json
from pathlib import Path

from services.agent.baseline import decide

CASES = Path(__file__).resolve().parents[2] / "evals" / "order_cases.json"


def load_cases(path: Path | None = None) -> list[dict]:
    return json.loads((path or CASES).read_text(encoding="utf-8"))


def evaluate(cases: list[dict] | None = None) -> dict:
    rows = cases if cases is not None else load_cases()
    correct = 0
    details = []
    for case in rows:
        actual = decide(case["context"])
        ok = _actions_match(actual["actions"], case["expected"]["actions"])
        correct += int(ok)
        details.append({"id": case["id"], "correct": ok})
    total = len(rows) or 1
    return {
        "cases": len(rows),
        "correct_action_rate": correct / total,
        "details": details,
        "overrides_excluded": True,
    }


def _actions_match(actual: list[dict], expected: list[dict]) -> bool:
    if len(actual) != len(expected):
        return False
    for got, want in zip(actual, expected):
        if got["type"] != want["type"]:
            return False
        params = got.get("params") or {}
        for key in ("supplier", "product", "quantity"):
            if key in want and params.get(key) != want[key]:
                return False
    return True
