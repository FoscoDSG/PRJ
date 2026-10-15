"""Evidence digest. HMAC of the cleartext facts, identical for every role.

The key lives only in ``IEOL_EVIDENCE_HMAC_KEY``. The hash is computed inside
the database by ``app.hmac_evidence``, which reads the cleartext column the
application role cannot select and returns the digest alone.
"""

from __future__ import annotations

import os

import psycopg


def evidence_key() -> str:
    value = os.environ.get("IEOL_EVIDENCE_HMAC_KEY")
    if value is None or value.strip() == "":
        raise RuntimeError(
            "IEOL_EVIDENCE_HMAC_KEY assente: avvio rifiutato. "
            "La chiave resta sul server e non va nel client."
        )
    return value


def evidence_hmac(conn: psycopg.Connection, fact_ids: list[str]) -> str:
    """Digest of the cleartext facts. The same list yields the same digest for every role."""
    row = conn.execute(
        "SELECT app.hmac_evidence(%s::uuid[], %s) AS digest",
        (fact_ids, evidence_key()),
    ).fetchone()
    if row is None or not row["digest"]:
        raise RuntimeError("Digest delle evidenze assente")
    return str(row["digest"])
