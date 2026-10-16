"""Provisional score distribution on headers that never entered the synonym file.

Shopify and FatturaPA are held out. The rows are not human labels. With fewer
than three sources the mapping threshold stays unmeasured and the fallback
is 0.75. This module does not write mapping decisions or thresholds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from services.mapping.engine import THRESHOLD, assigned_mapping, detect_profile
from services.mapping.headers import normalize_header
from services.testset import MIN_MEASURED_CASES, MIN_MEASURED_SOURCES

ROOT = Path(__file__).resolve().parents[2]
HOLDOUT_PATH = ROOT / "evals" / "holdout_headers.yaml"


def load_holdout() -> dict[str, Any]:
    raw = yaml.safe_load(HOLDOUT_PATH.read_text(encoding="utf-8"))
    if raw.get("label") != "PROVVISORIA":
        raise ValueError("La misura hold-out deve essere etichettata PROVVISORIA")
    if raw.get("calibrated") is not False:
        raise ValueError("La misura hold-out non è calibrata")
    if raw.get("human_labels") is not False:
        raise ValueError("Le intestazioni hold-out non sono etichette umane")
    return raw


def holdout_report() -> dict[str, Any]:
    """Score each held-out column. Does not select or store a threshold."""
    raw = load_holdout()
    rows: list[dict[str, Any]] = []
    for source in raw["sources"]:
        headers = list(source["headers"])
        profile = detect_profile(headers)
        for header in headers:
            target_type, target_property, score, rule = assigned_mapping(profile, header)
            rows.append(
                {
                    "source": source["id"],
                    "header": header,
                    "normalized": normalize_header(header),
                    "profile": profile,
                    "target_type": target_type,
                    "target_property": target_property,
                    "score": score,
                    "rule": rule,
                    "above_threshold": score >= THRESHOLD,
                }
            )
    source_count = len(raw["sources"])
    human_labels = False
    case_count = 0
    measured = (
        human_labels
        and case_count >= MIN_MEASURED_CASES
        and source_count >= MIN_MEASURED_SOURCES
    )
    above = sum(1 for row in rows if row["above_threshold"])
    below = len(rows) - above
    return {
        "label": "PROVVISORIA",
        "calibrated": False,
        "measured": measured,
        "threshold_status": "non misurata" if not measured else "misurata",
        "threshold": THRESHOLD,
        "source_count": source_count,
        "case_count": case_count,
        "human_labels": human_labels,
        "minimum_sources": MIN_MEASURED_SOURCES,
        "minimum_cases": MIN_MEASURED_CASES,
        "above": above,
        "below": below,
        "columns": rows,
        "reason": (
            f"La misura hold-out usa {source_count} fonti, sotto il minimo di "
            f"{MIN_MEASURED_SOURCES}, e non sono etichette umane. "
            "La soglia resta non misurata. "
            f"Vale il ripiego {THRESHOLD:.2f} (calibrated=false)."
        ),
    }
