"""Stub model. The decision rules live in the baseline, which the eval measures directly."""

from __future__ import annotations

import json
from typing import Any

from services.agent.baseline import decide


class StubOrderModel:
    def complete_structured(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del schema
        context = json.loads(prompt.split("CONTEXT_JSON=", 1)[1])
        return decide(context)
