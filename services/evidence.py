"""Evidence digest. HMAC of the cleartext facts, identical for every role.

The key lives in ``app.evidence_hmac_key``, written by the owner from
``IEOL_EVIDENCE_HMAC_KEY``. ``ieol_app`` cannot read or replace that row and
cannot pass a key into ``app.hmac_evidence``: the function takes only fact ids.
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


def install_evidence_key(conn: psycopg.Connection) -> None:
    """Store the server key. The caller must be the owner, not ``ieol_app``."""
    key = os.environ.get("IEOL_EVIDENCE_HMAC_KEY", "").strip()
    if key == "":
        return
    row = conn.execute("SELECT to_regclass('app.evidence_hmac_key') AS rel").fetchone()
    if row is None or row["rel"] is None:
        return
    conn.execute(
        """
        INSERT INTO app.evidence_hmac_key (id, secret)
        VALUES (1, %s)
        ON CONFLICT (id) DO UPDATE SET secret = EXCLUDED.secret
        """,
        (key,),
    )


def evidence_hmac(conn: psycopg.Connection, fact_ids: list[str]) -> str:
    """Digest of the cleartext facts. The same list yields the same digest for every role."""
    evidence_key()
    row = conn.execute(
        "SELECT app.hmac_evidence(%s::uuid[]) AS digest",
        (fact_ids,),
    ).fetchone()
    if row is None or not row["digest"]:
        raise RuntimeError("Digest delle evidenze assente")
    return str(row["digest"])
