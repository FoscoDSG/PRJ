"""Mapping threshold. 0.75 is the fallback until a measured value is stored."""

from __future__ import annotations

import psycopg

from services.mapping.engine import THRESHOLD

FALLBACK_THRESHOLD = THRESHOLD


def active_threshold(conn: psycopg.Connection) -> tuple[float, bool, int]:
    """Return ``(threshold, measured, support)``.

    A stored row counts only when ``measured`` is true. Otherwise the
    fallback 0.75 stays in force. ``measured`` is not a calibration:
    an F1-optimal threshold is still a rule score.
    """
    row = conn.execute(
        """
        SELECT threshold, measured, support
        FROM app.mapping_thresholds
        ORDER BY chosen_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None or not row["measured"]:
        return float(FALLBACK_THRESHOLD), False, 0
    return float(row["threshold"]), True, int(row["support"])
