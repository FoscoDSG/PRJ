"""Evidence digest. HMAC of the cleartext facts, identical for every role.

The key lives in ``app.evidence_hmac_key``, written by the owner from
``IEOL_EVIDENCE_HMAC_KEY``. ``ieol_app`` cannot read or replace that row and
cannot pass a key into ``app.hmac_evidence``: the function takes only fact ids.
"""

from __future__ import annotations

import os

import psycopg

from services.audit import record


def evidence_key() -> str:
    value = os.environ.get("IEOL_EVIDENCE_HMAC_KEY")
    if value is None or value.strip() == "":
        raise RuntimeError(
            "IEOL_EVIDENCE_HMAC_KEY assente: avvio rifiutato. "
            "La chiave resta sul server e non va nel client."
        )
    return value


def install_evidence_key(conn: psycopg.Connection, *, rotate: bool = False) -> None:
    """Store the server key. The caller must be the owner, not ``ieol_app``.

    Inserts the row when it is missing. Refuses to replace a different
    secret unless ``rotate`` is true. The table stays one row.
    """
    key = os.environ.get("IEOL_EVIDENCE_HMAC_KEY", "").strip()
    if key == "":
        return
    row = conn.execute("SELECT to_regclass('app.evidence_hmac_key') AS rel").fetchone()
    if row is None or row["rel"] is None:
        return
    stored = conn.execute(
        "SELECT secret FROM app.evidence_hmac_key WHERE id = 1"
    ).fetchone()
    if stored is None:
        conn.execute(
            "INSERT INTO app.evidence_hmac_key (id, secret) VALUES (1, %s)",
            (key,),
        )
        return
    if stored["secret"] == key:
        return
    if not rotate:
        raise RuntimeError(
            "IEOL_EVIDENCE_HMAC_KEY è diversa dalla chiave già in app.evidence_hmac_key. "
            "ieol migrate non la sostituisce: le evidenze già firmate non verrebbero più riconosciute. "
            "Per sovrascrivere l'unica riga: ieol migrate --rotate-evidence-key."
        )
    conn.execute(
        "UPDATE app.evidence_hmac_key SET secret = %s WHERE id = 1",
        (key,),
    )
    actor = conn.execute("SELECT current_user AS actor").fetchone()["actor"]
    record(
        conn,
        action="evidence_key_rotate",
        actor=actor,
        role="owner",
        entity_ref="app.evidence_hmac_key",
        payload={},
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
