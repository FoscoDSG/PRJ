"""Learned corrections stay inside one company and out of the recognition metric."""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from services.db import connect, password_hash
from services.mapping.corrections import active_correction, file_type_metric, learned_metric, present_origin
from services.mapping.engine import propose_mappings
from services.testset import _human_mapping_cases, build_testset, evaluate_testset
from tests.test_flow import load_demo, login

ROOT = Path(__file__).resolve().parents[1]
ROME = ZoneInfo("Europe/Rome")
ALTRO_PASSWORD = "altra-password-not-role"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_null_company_row_is_shown_as_a_rule_without_a_revoke():
    shown = present_origin(
        {
            "company_id": None,
            "origin": "learned",
            "source_decision_id": "legacy",
            "confidence": 0.99,
        },
        {
            "company_id": "azienda",
            "actor": "ada",
            "decided_at": datetime(2026, 10, 17, 12, 0, tzinfo=ROME),
        },
        viewer_company="azienda",
    )
    assert shown["origin"] == "rule"
    assert shown["origin_text"] == "Da regola"
    assert shown["decision_id"] is None
    assert shown["actor"] is None
    assert shown["decided_at"] is None


def test_correction_applies_only_for_the_same_company_source_and_header():
    company_a, company_b = _companies()
    with connect() as conn:
        _user(conn, "ada", company_a)
        host = _mapping(conn, company_id=company_a, source="learnme", field="COD_ART")
        correction = _decision(
            conn,
            host,
            decision="reject",
            company_id=company_a,
            actor="ada",
            source_name="learnme",
            source_field="COD_ART",
            corrected="unit_price",
        )
        legacy_host = _mapping(conn, company_id=None, source="legacy", field="COD_ART")
        _decision(
            conn,
            legacy_host,
            decision="reject",
            company_id=None,
            actor="ada",
            source_name="legacy",
            source_field="COD_ART",
            corrected="unit_price",
        )
        conn.commit()
        found = active_correction(conn, company_id=company_a, source_name="learnme", header="cod art")
        assert found is not None
        assert found["id"] == correction
        assert found["target_property_corrected"] == "unit_price"
        assert active_correction(conn, company_id=company_b, source_name="learnme", header="cod art") is None
        assert active_correction(conn, company_id=company_a, source_name="legacy", header="cod art") is None
        assert active_correction(conn, company_id=company_a, source_name="learnme", header="qty") is None
        assert active_correction(conn, company_id=company_a, source_name="altra", header="cod art") is None
        learned = propose_mappings(
            conn,
            source_name="learnme",
            columns=["Cod Art"],
            rows=[{"Cod Art": "ART-1"}],
            company_id=company_a,
            profile="order_line",
        )
        learned_row = learned["mappings"][0]
        assert learned_row["origin"] == "learned"
        assert learned_row["decision_id"] == correction
        assert learned_row["target_property"] == "unit_price"
        other = propose_mappings(
            conn,
            source_name="learnme",
            columns=["Cod Art", "COD ART"],
            rows=[{"Cod Art": "ART-1", "COD ART": "ART-2"}],
            company_id=company_b,
            profile="order_line",
        )
        created = {item["source_field"]: item for item in other["mappings"]}
        assert "Cod Art" not in created
        assert created["COD ART"]["origin"] == "rule"
        assert created["COD ART"]["target_property"] == "product_source_key"
        assert created["COD ART"]["decision_id"] is None


def test_user_cannot_see_or_revoke_another_companys_decisions(client: TestClient):
    company_a, company_b = _companies()
    with connect() as conn:
        _user(conn, "altra", company_b)
        conn.commit()

    login(client, "mapper")
    missing = str(uuid.uuid4())
    denied = client.post(f"/api/mapping-decisions/{missing}/revoke", json={})
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Azienda non assegnata"
    untyped = client.post(f"/api/files/{missing}/type", json={"decision": "corretto"})
    assert untyped.status_code == 403
    assert untyped.json()["detail"] == "Azienda non assegnata"
    client.post("/api/logout")
    login(client, "operator")
    forbidden = client.post(f"/api/mapping-decisions/{missing}/revoke", json={})
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "Serve il ruolo mapper"
    client.post("/api/logout")

    with connect() as conn:
        conn.execute(
            "UPDATE app.users SET company_id = %s WHERE username = 'mapper'",
            (company_a,),
        )
        conn.commit()

    login(client, "mapper")
    load_demo()
    listed = client.get("/api/mappings").json()["mappings"]
    legacy = next(item for item in listed if item["source_name"] == "righe" and item["source_field"] == "cod_art")
    assert legacy["origin"] == "rule"
    assert legacy["origin_text"] == "Da regola"
    assert legacy["decision_id"] is None
    assert legacy["actor"] is None
    rejected = client.post(
        f"/api/mappings/{legacy['id']}/reject",
        params={"target_property_corrected": "unit_price"},
    )
    assert rejected.status_code == 200, rejected.text
    correction_id = rejected.json()["decision_id"]

    uploaded = client.post(
        "/api/ingest/csv",
        files={"file": ("righe_nuove.csv", _csv("ORD_HEAD,COD_ART,QTY,PREZZO", "1024,ART-X,501,120"), "text/csv")},
        data={"source_name": "righe", "source_class": "system_of_record", "company_id": company_b},
    )
    assert uploaded.status_code == 200, uploaded.text
    righe_rows = _fields(client, "righe", "cod_art")
    learned = next(item for item in righe_rows if item["origin"] == "learned")
    assert learned["decision_id"] == correction_id
    assert learned["actor"] == "mapper"
    assert learned["target_property"] == "unit_price"
    assert float(learned["confidence"]) >= 0.75
    assert learned["band"] == "verificare"
    decided = datetime.fromisoformat(learned["decided_at"])
    day = decided.astimezone(ROME).strftime("%d/%m/%Y")
    assert learned["origin_text"] == f"Da una correzione di mapper del {day}"
    legacy_again = next(item for item in righe_rows if item["id"] == legacy["id"])
    assert legacy_again["origin"] == "rule"
    assert legacy_again["origin_text"] == "Da regola"
    assert legacy_again["decision_id"] is None

    bulk = client.post("/api/mappings/confirm-confident")
    assert bulk.status_code == 200, bulk.text
    assert next(item for item in _fields(client, "righe", "cod_art") if item["id"] == learned["id"])["status"] == "proposed"

    poisoned = client.post(
        "/api/ingest/sql",
        json={"table": "src.clienti", "source_class": "system_of_record", "company_id": company_b},
    )
    assert poisoned.status_code == 422
    solo = client.post(
        "/api/ingest/csv",
        files={"file": ("solo.csv", _csv("ORD_HEAD,COD_ART,QTY,PREZZO", "9,ART-Z,1,3"), "text/csv")},
        data={"source_name": "solo_alfa", "source_class": "external", "company_id": company_b},
    )
    assert solo.status_code == 200, solo.text
    solo_file = solo.json()["batch_id"]
    solo_row = _field(client, "solo_alfa", "cod_art")
    with connect() as conn:
        stored_company = conn.execute(
            "SELECT company_id::text AS company_id FROM app.field_mappings WHERE id = %s",
            (solo_row["id"],),
        ).fetchone()["company_id"]
    assert stored_company == company_a

    typed = client.post(f"/api/files/{solo_file}/type", json={"decision": "corretto", "company_id": company_b})
    assert typed.status_code == 422
    typed = client.post(f"/api/files/{solo_file}/type", json={"decision": "corretto"})
    assert typed.status_code == 200, typed.text
    bad = client.post(
        f"/api/files/{solo_file}/type",
        json={"decision": "sbagliato", "correct_type": "inventato"},
    )
    assert bad.status_code == 422
    unknown = client.post(f"/api/files/{solo_file}/type", json={"decision": "non_so"})
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["decision"] == "non_so"
    assert unknown.json()["correct_type"] is None
    with connect() as conn:
        batch_score = conn.execute(
            "SELECT proposed_score FROM app.landing_batches WHERE id = %s",
            (solo_file,),
        ).fetchone()["proposed_score"]
        stored = conn.execute(
            """
            SELECT decision, correct_type, score
            FROM app.table_type_decisions
            WHERE id = %s
            """,
            (unknown.json()["id"],),
        ).fetchone()
    assert stored["decision"] == "non_so"
    assert stored["correct_type"] is None
    assert float(unknown.json()["score"]) == float(batch_score)
    assert float(stored["score"]) == float(batch_score)
    wrong = client.post(
        f"/api/files/{solo_file}/type",
        json={"decision": "sbagliato", "correct_type": "product"},
    )
    assert wrong.status_code == 200, wrong.text
    with connect() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(
                "UPDATE app.table_type_decisions SET decision = 'corretto' WHERE file_id = %s",
                (solo_file,),
            )
            conn.commit()
        conn.rollback()

    proposed = next(
        item for item in client.get("/api/mappings").json()["mappings"]
        if item["status"] == "proposed" and item["origin"] != "learned" and item["source_name"] != "righe"
    )
    confirmed = client.post(
        f"/api/mappings/{proposed['id']}/confirm",
        params={"company_id": company_b},
    )
    assert confirmed.status_code == 200, confirmed.text
    with connect() as conn:
        decision_company = conn.execute(
            "SELECT company_id::text AS company_id FROM app.mapping_decisions WHERE id = %s",
            (confirmed.json()["decision_id"],),
        ).fetchone()["company_id"]
    assert decision_company == company_a

    client.post("/api/logout")
    client.post("/api/login", json={"username": "altra", "password": ALTRO_PASSWORD})
    hidden = client.get("/api/mappings", params={"company_id": company_a}).json()
    assert all(item["id"] != learned["id"] for item in hidden["mappings"])
    assert all(item["source_name"] != "solo_alfa" for item in hidden["mappings"])
    assert correction_id not in json.dumps(hidden)
    assert all(item["source_name"] != "solo_alfa" for item in hidden["files"])
    cannot_revoke = client.post(f"/api/mapping-decisions/{correction_id}/revoke", json={})
    assert cannot_revoke.status_code == 404
    cannot_smuggle = client.post(
        f"/api/mapping-decisions/{correction_id}/revoke",
        json={"company_id": company_a},
    )
    assert cannot_smuggle.status_code == 422
    cannot_type = client.post(f"/api/files/{solo_file}/type", json={"decision": "corretto"})
    assert cannot_type.status_code == 404

    client.post("/api/logout")
    login(client, "mapper")
    revoked = client.post(f"/api/mapping-decisions/{correction_id}/revoke", json={})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["decision"] == "annullata"
    assert revoked.json()["revoked_decision_id"] == correction_id
    with connect() as conn:
        original = conn.execute(
            "SELECT decision FROM app.mapping_decisions WHERE id = %s",
            (correction_id,),
        ).fetchone()
        assert original["decision"] == "reject"
        reset = conn.execute(
            """
            SELECT origin, source_decision_id, target_property, status
            FROM app.field_mappings
            WHERE id = %s
            """,
            (learned["id"],),
        ).fetchone()
        assert reset["origin"] == "rule"
        assert reset["source_decision_id"] is None
        assert reset["target_property"] == "product_source_key"
        assert reset["status"] == "proposed"
        assert active_correction(conn, company_id=company_a, source_name="righe", header="cod art") is None
        kinds = {
            row["payload"]["kind"]
            for row in conn.execute(
                "SELECT payload FROM app.audit_log WHERE action = 'human_decision'"
            ).fetchall()
        }
    assert "mapping_correction_revoked" in kinds
    assert "table_type_decision" in kinds
    visible = next(item for item in _fields(client, "righe", "cod_art") if item["id"] == learned["id"])
    assert visible["origin"] == "rule"
    assert visible["origin_text"] == "Da regola"
    assert visible["decision_id"] is None


def test_measurement_keeps_first_sight_and_counts_learned_apart():
    company_c, company_d = _companies()
    with connect() as conn:
        _user(conn, "misura", company_c)
        first = _mapping(
            conn, company_id=company_c, source="alfa", field="cod_art", valid_from="2020-01-01",
        )
        later = _mapping(
            conn, company_id=company_c, source="alfa", field="qty", valid_from="2024-06-01",
        )
        other = _mapping(
            conn, company_id=company_d, source="alfa", field="cod_art", valid_from="2021-01-01",
        )
        archive = _mapping(conn, company_id=company_c, source="archivio", field="COD_ART")
        correction = _decision(
            conn,
            archive,
            decision="reject",
            company_id=company_c,
            actor="misura",
            source_name="archivio",
            source_field="COD_ART",
            corrected="unit_price",
        )
        learned_map = _mapping(
            conn,
            company_id=company_c,
            source="beta",
            field="Cod Art",
            origin="learned",
            source_decision_id=correction,
            target_property="unit_price",
        )
        rejected_map = _mapping(
            conn,
            company_id=company_c,
            source="gamma",
            field="Cod Art",
            origin="learned",
            source_decision_id=correction,
            target_property="unit_price",
        )
        bulk_map = _mapping(conn, company_id=company_c, source="massa", field="cod_art")
        first_id = _decision(
            conn, first, decision="confirm", company_id=company_c, actor="misura",
            source_name="alfa", source_field="cod_art", origin="rule",
        )
        later_id = _decision(
            conn, later, decision="confirm", company_id=company_c, actor="misura",
            source_name="alfa", source_field="qty", origin="rule",
        )
        other_id = _decision(
            conn, other, decision="confirm", company_id=company_d, actor="misura",
            source_name="alfa", source_field="cod_art", origin="rule",
        )
        learned_confirm = _decision(
            conn, learned_map, decision="confirm", company_id=company_c, actor="misura",
            source_name="beta", source_field="Cod Art", origin="learned", source_decision_id=correction,
        )
        learned_reject = _decision(
            conn, rejected_map, decision="reject", company_id=company_c, actor="misura",
            source_name="gamma", source_field="Cod Art", origin="learned", source_decision_id=correction,
        )
        bulk_id = _decision(
            conn, bulk_map, decision="confirm", company_id=company_c, actor="misura",
            source_name="massa", source_field="cod_art", method="bulk_rule", origin="rule",
        )
        revoked = _decision(
            conn, archive, decision="annullata", company_id=company_c, actor="misura",
            source_name="archivio", source_field="COD_ART", revoked_decision_id=correction,
        )
        file_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO app.landing_batches (
              id, source_name, watermark, row_count, proposed_type, proposed_score
            )
            VALUES (%s, 'archivio', 'wm', 1, 'order_line', 0.81)
            """,
            (file_id,),
        )
        for decision, correct in (("corretto", None), ("sbagliato", "product"), ("non_so", None)):
            conn.execute(
                """
                INSERT INTO app.table_type_decisions (
                  id, company_id, file_id, proposed_type, score, decision, correct_type, actor, actor_role
                )
                VALUES (%s, %s, %s, 'order_line', 0.81, %s, %s, 'misura', 'mapper')
                """,
                (str(uuid.uuid4()), company_c, file_id, decision, correct),
            )
        conn.commit()
        labels = {item["decision_id"] for item in _human_mapping_cases(conn)}
        assert first_id in labels
        assert other_id in labels
        assert correction in labels
        assert later_id not in labels
        assert learned_confirm not in labels
        assert learned_reject not in labels
        assert bulk_id not in labels
        assert revoked not in labels
        assert learned_metric(conn) == {"confirmed": 1, "rejected": 1, "revoked": 1}
        typed = file_type_metric(conn)
        assert typed["non_so"] == 1
        assert typed["sbagliato"] == 1
        assert typed["errors"] == typed["sbagliato"] + typed["non_so"]
        assert typed["learning_examples"] == typed["sbagliato"]
        build_testset(conn)
        stored = {
            row["mapping_decision_id"]
            for row in conn.execute(
                """
                SELECT mapping_decision_id::text AS mapping_decision_id
                FROM app.labelled_cases
                WHERE case_kind = 'mapping' AND label_origin = 'human_decision'
                """
            ).fetchall()
        }
        assert stored == labels
        report = evaluate_testset(conn)
        assert report["groups"]["mapping"]["learned"] == {"confirmed": 1, "rejected": 1, "revoked": 1}
        assert report["groups"]["mapping"]["file_type"]["non_so"] == 1
        assert report["groups"]["mapping"]["file_type"]["errors"] == 2
        assert report["groups"]["mapping"]["file_type"]["learning_examples"] == 1
        conn.commit()


def test_database_refuses_a_revoke_or_file_type_from_another_company():
    company_a, company_b = _companies()
    with connect() as conn:
        _user(conn, "ada", company_a)
        _user(conn, "bea", company_b)
        host = _mapping(conn, company_id=company_a, source="isolata", field="COD_ART")
        correction = _decision(
            conn,
            host,
            decision="reject",
            company_id=company_a,
            actor="ada",
            source_name="isolata",
            source_field="COD_ART",
            corrected="unit_price",
        )
        file_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO app.landing_batches (
              id, source_name, watermark, row_count, proposed_type, proposed_score
            )
            VALUES (%s, 'isolata', 'wm', 1, 'order', 0.5)
            """,
            (file_id,),
        )
        conn.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="altra azienda"):
            _decision(
                conn, host, decision="annullata", company_id=company_b, actor="bea",
                source_name="isolata", source_field="COD_ART", revoked_decision_id=correction,
            )
            conn.commit()
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="altra azienda"):
            conn.execute(
                """
                INSERT INTO app.table_type_decisions (
                  id, company_id, file_id, proposed_type, score, decision, actor, actor_role
                )
                VALUES (%s, %s, %s, 'order', 0.5, 'corretto', 'bea', 'mapper')
                """,
                (str(uuid.uuid4()), company_a, file_id),
            )
            conn.commit()
        conn.rollback()


def test_0010_changes_the_live_comment_and_does_not_rewrite_rows():
    old = (ROOT / "db" / "migrations" / "0007_mapping_decisions_key_audit.sql").read_text(encoding="utf-8")
    assert "Non è un dataset che l''agente legge per proporre." in old
    script = (ROOT / "db" / "migrations" / "0010_companies_learned_corrections.sql").read_text(encoding="utf-8")
    code = "\n".join(line.split("--", 1)[0] for line in script.splitlines())
    assert "UPDATE app." not in code
    agent = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "services" / "agent").glob("*.py"))
    assert "mapping_decisions" not in agent
    assert "table_type_decisions" not in (ROOT / "services" / "mapping" / "engine.py").read_text(encoding="utf-8")
    with connect() as conn:
        comment = conn.execute(
            "SELECT obj_description('app.mapping_decisions'::regclass) AS comment"
        ).fetchone()["comment"]
    assert "0007" in comment
    assert "0010" in comment
    assert "l'agente legge per proporre" in comment


def test_review_markup_uses_the_server_phrase_and_hides_revoke_on_a_rule():
    page = (ROOT / "apps" / "web" / "index.html").read_text(encoding="utf-8")
    button = page.split('id="confirm-high"', 1)[1].split("</button>", 1)[0]
    assert "Conferma i suggerimenti forti" in button
    assert "soglia" not in button.casefold()
    script = (ROOT / "apps" / "web" / "app.js").read_text(encoding="utf-8")
    assert "decided_at" not in script
    assert "soglia" not in script.casefold()
    assert "sicuro" not in script.casefold()
    assert "garantito" not in script.casefold()
    rendered = subprocess.run(
        ["node", "-e", _MARKUP_CHECK],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == "ok\n"


def _companies() -> tuple[str, str]:
    company_a = str(uuid.uuid4())
    company_b = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            "INSERT INTO app.companies (id, name) VALUES (%s, 'Alfa'), (%s, 'Beta')",
            (company_a, company_b),
        )
        conn.commit()
    return company_a, company_b


def _user(conn, username: str, company_id: str) -> None:
    conn.execute(
        """
        INSERT INTO app.users (username, password_hash, role, company_id)
        VALUES (%s, %s, 'mapper', %s)
        """,
        (username, password_hash(ALTRO_PASSWORD if username == "altra" else f"{username}-secret"), company_id),
    )


def _mapping(
    conn,
    *,
    company_id: str | None,
    source: str,
    field: str,
    origin: str = "rule",
    source_decision_id: str | None = None,
    target_property: str = "product_source_key",
    valid_from: str | None = None,
) -> str:
    mapping_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.field_mappings (
          id, source_name, source_field, target_type, target_property,
          confidence, status, profile, company_id, origin, source_decision_id, valid_from
        )
        VALUES (%s, %s, %s, 'OrderLine', %s, 0.99, 'proposed', 'order_line', %s, %s, %s, coalesce(%s::timestamptz, now()))
        """,
        (mapping_id, source, field, target_property, company_id, origin, source_decision_id, valid_from),
    )
    return mapping_id


def _decision(
    conn,
    mapping_id: str,
    *,
    decision: str,
    company_id: str | None,
    actor: str,
    source_name: str,
    source_field: str,
    corrected: str | None = None,
    origin: str | None = None,
    source_decision_id: str | None = None,
    revoked_decision_id: str | None = None,
    method: str = "human",
) -> str:
    decision_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.mapping_decisions (
          id, mapping_id, decision, confidence_method, actor, actor_role,
          source_name, source_field, target_property, target_property_corrected,
          score, confidence_rule, company_id, origin, source_decision_id, revoked_decision_id
        )
        VALUES (%s, %s, %s, %s, %s, 'mapper', %s, %s, 'product_source_key', %s, 0.80, 'mapping.header_similarity@v1', %s, %s, %s, %s)
        """,
        (
            decision_id,
            mapping_id,
            decision,
            method,
            actor,
            source_name,
            source_field,
            corrected,
            company_id,
            origin,
            source_decision_id,
            revoked_decision_id,
        ),
    )
    return decision_id


def _fields(client: TestClient, source_name: str, source_field: str) -> list[dict]:
    rows = client.get("/api/mappings").json()["mappings"]
    return [item for item in rows if item["source_name"] == source_name and item["source_field"] == source_field]


def _field(client: TestClient, source_name: str, source_field: str) -> dict:
    return _fields(client, source_name, source_field)[0]


def _csv(header: str, row: str) -> bytes:
    return f"{header}\n{row}\n".encode()


_MARKUP_CHECK = r"""
const { mappingRow } = require("./apps/web/escape.js");
const learned = mappingRow({
  id: "1",
  source_name: "righe",
  source_field: "Cod Art",
  target_type: "OrderLine",
  target_property: "unit_price",
  confidence: 0.99,
  status: "proposed",
  origin: "learned",
  origin_text: "Da una correzione di ada del 17/10/2026",
  decision_id: "dec-1",
  band: "forte",
  band_note: "sicuro",
});
if (!learned.includes("Da una correzione di ada del 17/10/2026")) process.exit(1);
if (!learned.includes("Annulla correzione")) process.exit(2);
if (learned.includes("0.99")) process.exit(3);
if (learned.includes("sicuro")) process.exit(4);
const rule = mappingRow({
  id: "2",
  source_name: "righe",
  source_field: "COD_ART",
  target_type: "OrderLine",
  target_property: "product_source_key",
  confidence: 0.99,
  status: "proposed",
  origin: "rule",
  decision_id: null,
  band: "forte",
  band_note: "garantito",
});
if (!rule.includes("Da regola")) process.exit(5);
if (rule.includes("Annulla correzione")) process.exit(6);
if (!rule.includes("Suggerimento forte")) process.exit(7);
if (!rule.includes("Indicazione automatica, da verificare")) process.exit(8);
if (rule.includes("garantito") || rule.includes("sicuro")) process.exit(9);
const weak = mappingRow({
  id: "3",
  source_name: "x",
  source_field: "y",
  target_type: "Document",
  target_property: "title",
  confidence: 0.2,
  status: "proposed",
  origin: "rule",
  band: "verificare",
});
if (!weak.includes("Da verificare")) process.exit(10);
if (weak.includes("Indicazione automatica")) process.exit(11);
console.log("ok");
"""
