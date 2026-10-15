"""Mapping threshold. 0.75 is the fallback until a measured value is stored."""

from __future__ import annotations

import psycopg

from services.mapping.engine import THRESHOLD

FALLBACK_THRESHOLD = THRESHOLD


def active_threshold(conn: psycopg.Connection) -> tuple[float, bool]:
    """Return ``(threshold, measured)``.

    A stored row counts only when ``measured`` is true. Otherwise the
    fallback 0.75 stays in force and is not a calibrated probability.
    """
    row = conn.execute(
        """
        SELECT threshold, measured
        FROM app.mapping_thresholds
        ORDER BY chosen_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None or not row["measured"]:
        return float(FALLBACK_THRESHOLD), False
    return float(row["threshold"]), True
