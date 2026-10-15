"""PII masking, readable audit, labelled test set, measured threshold."""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from services.db import app_connect, connect, pii_connect
from services.mapping.engine import HEADER_SIMILARITY_RULE
from services.pii import MASK
from services.testset import MIN_MEASURED_CASES, build_testset, evaluate_testset, load_hand_labels
from services.text import digest
from tests.test_flow import decide, load_demo, login

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
CLEARTEXT = (
    "ordini@acme.example",
    "buyer@bianchi.example",
    "buyer@nuova.example",
    "IT12345678901",
    "IT00987654321",
    "IT55555555555",
    "Acme Srl",
    "ACME SRL",
    "Bianchi SpA",
    "Nuova Srl",
    "Fornitore A",
    "Fornitore B",
    "Fornitore C",
)


def _clean(payload) -> None:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    for secret in CLEARTEXT:
        assert secret not in text


def test_ieol_app_cannot_select_cleartext_columns():
    statements = (
        "SELECT value_cleartext FROM app.facts",
        "SELECT data_cleartext FROM app.landing_rows",
        "SELECT samples_cleartext FROM app.field_mappings",
        "SELECT recipient_cleartext FROM app.email_outbox",
        "SELECT ragione_sociale, piva, email FROM src.clienti",
        "SELECT nome FROM src.fornitori",
        "UPDATE app.facts SET value = value_cleartext",
    )
    with app_connect() as conn:
        for statement in statements:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()
    with pii_connect() as conn:
        conn.execute("SELECT value_cleartext FROM app.facts")
        conn.execute("SELECT ragione_sociale FROM src.clienti")
        conn.rollback()


def test_api_hides_cleartext_and_unmask_is_audited(client: TestClient):
    login(client, "mapper")
    load_demo()
    paths = ("/api/inbox", "/api/orders", "/api/mappings", "/api/audit")
    for role in ("mapper", "operator", "approver", "admin"):
        login(client, role)
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, response.text
            _clean(response.json())
        orders = client.get("/api/orders").json()["orders"]
        order = next(item for item in orders if item["business_key"] == "1024")
        _clean(client.get(f"/api/orders/{order['id']}").json())
        _clean(client.get(f"/api/customers/{order['customer_id']}").json())
    login(client, "operator")
    denied = client.post(
        "/api/pii/unmask",
        json={"entity_id": order["customer_id"], "reason": "verifica operativa"},
    )
    assert denied.status_code == 403
    login(client, "admin")
    opened = client.post(
        "/api/pii/unmask",
        json={"entity_id": order["customer_id"], "reason": "verifica del cliente"},
    )
    assert opened.status_code == 200, opened.text
    values = {item["property"]: item["value"] for item in opened.json()["properties"]}
    assert values["vat"] == "IT12345678901"
    assert "IT12345678901" not in json.dumps(client.get("/api/audit").json())
    again = client.get(f"/api/customers/{order['customer_id']}").json()
    assert again["company_name"] == MASK
    _clean(again)


def test_evidence_hash_is_role_independent_hmac(client: TestClient):
    login(client, "mapper")
    load_demo()
    login(client, "operator")
    operator = client.get("/api/inbox").json()["recommendations"][0]
    login(client, "approver")
    approver = client.get("/api/inbox").json()["recommendations"][0]
    assert operator["evidence_hash"] == approver["evidence_hash"]
    assert len(operator["evidence_hash"]) == 64
    assert operator["evidence_hash"] != digest(operator["evidence"])
    _clean(operator)
    _clean(approver)


def test_readable_audit_is_italian_readonly_and_filtered(client: TestClient):
    login(client, "mapper")
    load_demo()
    login(client, "approver")
    item = client.get("/api/inbox").json()["recommendations"][0]
    assert decide(client, item).status_code == 200
    with connect() as conn:
        before_audit = conn.execute("SELECT count(*) AS n FROM app.audit_log").fetchone()["n"]
        before_decisions = conn.execute("SELECT count(*) AS n FROM app.decisions_log").fetchone()["n"]
        conn.execute(
            """
            INSERT INTO app.audit_log (id, at, actor, role, action, entity_ref, payload)
            VALUES (%s, '2026-01-15 10:00:00+00', 'ada', 'admin', 'proposal', 'nessun-ordine', '{}')
            """,
            ("11111111-1111-1111-1111-111111111111",),
        )
        conn.execute(
            """
            INSERT INTO app.audit_log (id, at, actor, role, action, entity_ref, payload)
            VALUES (%s, '2026-10-15 10:00:00+00', 'ada', 'admin', 'proposal', 'nessun-ordine', '{}')
            """,
            ("22222222-2222-2222-2222-222222222222",),
        )
        conn.commit()
    login(client, "operator")
    denied = client.get("/api/audit/readable")
    assert denied.status_code == 403
    login(client, "approver")
    page = client.get("/api/audit/readable", params={"limit": 1, "offset": 0})
    assert page.status_code == 200, page.text
    body = page.json()
    assert body["limit"] == 1
    assert body["total"] > 1
    assert len(body["events"]) == 1
    _clean(body)
    january = client.get("/api/audit/readable", params={"user": "ada", "date_from": "2026-01-15", "date_to": "2026-01-15"})
    assert january.status_code == 200, january.text
    when = {event["when"] for event in january.json()["events"]}
    assert "15/01/2026 11:00:00" in when
    october = client.get("/api/audit/readable", params={"user": "ada", "date_from": "2026-10-15", "date_to": "2026-10-15"})
    october_event = october.json()["events"][0]
    assert october_event["when"] == "15/10/2026 12:00:00"
    assert october_event["what"] == "Proposta"
    assert october_event["who"] == "ada · amministratore"
    from services.audit_read import _event

    fallback = _event(
        {
            "kind": "audit",
            "code": "custom_event",
            "actor": "ada",
            "role": "admin",
            "when_rome": "15/10/2026 12:00:00",
            "order_key": None,
            "detail": "nota",
        }
    )
    assert fallback["what"] == "custom_event"
    labelled = client.get("/api/audit/readable", params={"order": "1024", "user": "approver"})
    assert labelled.status_code == 200, labelled.text
    assert labelled.json()["events"]
    assert any(event["what"] == "Approvazione" for event in labelled.json()["events"])
    assert all(event["order"] == "1024" for event in labelled.json()["events"])
    _clean(labelled.json())
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.audit_log").fetchone()["n"] == before_audit + 2
        assert conn.execute("SELECT count(*) AS n FROM app.decisions_log").fetchone()["n"] == before_decisions


def test_readable_card_escapes_every_field():
    import subprocess

    script = """
const { readableCard, escapeHtml } = require("./apps/web/escape.js");
const value = '"><img src=x onerror=alert(1)>';
const html = readableCard({ action: value, who: value, when: value, what: value, order: value, detail: value });
if (html.includes(value) || html.includes("<img")) process.exit(2);
if (!html.includes(escapeHtml(value))) process.exit(3);
process.stdout.write("ok\\n");
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"


def test_testset_fallback_until_minimum_and_separated_measurement(monkeypatch: pytest.MonkeyPatch):
    hand = load_hand_labels()
    assert hand
    with connect() as conn:
        built = build_testset(conn)
        report = evaluate_testset(conn)
        conn.commit()
    assert built["mapping_count"] < MIN_MEASURED_CASES
    threshold = report["threshold"]
    assert float(threshold["threshold"]) == 0.75
    assert threshold["method"] == "fallback"
    assert threshold["measured"] is False
    assert threshold["rules_version"] == HEADER_SIMILARITY_RULE
    assert report["groups"]["mapping"]["method"] == "too_small_for_split"
    assert report["groups"]["mapping"]["separated"] is False
    assert report["groups"]["mapping"]["n"] == 0
    assert report["groups"]["agent"]["n"] == report["groups"]["rules"]["n"]
    assert report["groups"]["agent"]["n"] >= 1
    assert report["groups"]["agent"]["errors"] == 0
    assert report["groups"]["rules"]["errors"] == report["groups"]["rules"]["n"]
    assert report["groups"]["agent"]["accuracy_low"] is not None
    assert report["groups"]["rules"]["accuracy_high"] is not None

    def four_mappings():
        return [
            {
                "case_kind": "mapping",
                "profile": "order",
                "source_field": "ord_head",
                "target_type": "Order",
                "target_property": "source_key",
            }
            for _ in range(4)
        ]

    monkeypatch.setattr("services.testset.load_hand_labels", four_mappings)
    with connect() as conn:
        build_testset(conn)
        partial = evaluate_testset(conn)
        conn.commit()
    assert partial["groups"]["mapping"]["method"] == "leave_one_out"
    assert partial["groups"]["mapping"]["separated"] is True
    assert partial["groups"]["mapping"]["n"] == 4
    assert partial["threshold"]["measured"] is False
    assert float(partial["threshold"]["threshold"]) == 0.75

    def enough():
        cases = [
            {
                "case_kind": "mapping",
                "profile": "order",
                "source_field": "ord_head",
                "target_type": "Order",
                "target_property": "source_key",
            }
            for _ in range(MIN_MEASURED_CASES)
        ]
        cases.append(
            {
                "case_kind": "recommendation",
                "context": {
                    "order_key": "ORD-H1",
                    "amount": 10,
                    "customer_key": "C1",
                    "lines": [{"product": "ART-1", "shortage": 1, "quantity": 1}],
                    "suppliers": [
                        {"source_key": "SUP-SLOW", "product": "ART-1", "delay_days": 9},
                        {"source_key": "SUP-FAST", "product": "ART-1", "delay_days": 1},
                    ],
                },
                "expected_actions": [
                    {
                        "type": "createPurchaseOrder",
                        "params": {
                            "supplier": "SUP-FAST",
                            "product": "ART-1",
                            "quantity": 1,
                            "order": "ORD-H1",
                        },
                    },
                    {"type": "notifyCustomer", "params": {"order": "ORD-H1", "customer": "C1"}},
                ],
            }
        )
        return cases

    monkeypatch.setattr("services.testset.load_hand_labels", enough)
    with connect() as conn:
        build_testset(conn)
        measured = evaluate_testset(conn)
        stored = conn.execute(
            """
            SELECT threshold, method, measured, rules_version
            FROM app.mapping_thresholds
            ORDER BY chosen_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        conn.commit()
    assert stored["measured"] is True
    assert stored["method"] == "kfold5_f1_v1"
    assert stored["rules_version"] == HEADER_SIMILARITY_RULE
    assert float(stored["threshold"]) == pytest.approx(0.98)
    assert measured["groups"]["mapping"]["separated"] is True
    assert measured["groups"]["mapping"]["n"] == MIN_MEASURED_CASES
    assert measured["groups"]["agent"]["n"] == measured["groups"]["rules"]["n"] == 1
    assert measured["groups"]["agent"]["errors"] == 0
    assert measured["groups"]["rules"]["errors"] == 1


def test_unknown_sources_and_agent_recommendations_are_not_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from services.errors import AppError

    load_demo()
    with connect() as conn:
        version = build_testset(conn)["version"]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO app.labelled_cases (
                  id, testset_version, case_kind, label_origin, decision_id, label
                )
                VALUES (gen_random_uuid(), %s, 'recommendation', 'agent', NULL, '{}')
                """,
                (version,),
            )
        conn.rollback()
        version = build_testset(conn)["version"]
        conn.execute(
            """
            INSERT INTO app.decisions_log (
              id, recommendation_id, batch_id, row_index, decision, evidence_hash,
              recommendation, confidence, actor, actor_role, agent_version, rules_version,
              confidence_threshold, amount_threshold_eur, action_label, context_snapshot
            )
            SELECT gen_random_uuid(), r.id, lr.batch_id, lr.row_index, 'approve', r.evidence_hash,
                   'non usare', 0.2, 'intruso', 'approver', 'x', 'y', 0.75, 1,
                   '[{"type":"notifyCustomer","params":{}}]'::jsonb, '{}'::jsonb
            FROM app.recommendations r
            JOIN app.landing_rows lr ON lr.data ->> 'ord_head' = '1024'
            LIMIT 1
            """
        )
        decision_id = conn.execute(
            "SELECT id FROM app.decisions_log WHERE actor = 'intruso'"
        ).fetchone()["id"]
        source = conn.execute(
            """
            SELECT batch_id, row_index FROM app.landing_rows lr
            WHERE lr.data ->> 'ord_head' = '1024'
            LIMIT 1
            """
        ).fetchone()
        conn.execute(
            """
            INSERT INTO app.decisions_log_sources (decision_id, batch_id, row_index, confidence_method)
            VALUES (%s, %s, %s, 'unknown')
            """,
            (decision_id, source["batch_id"], source["row_index"]),
        )
        second = build_testset(conn)
        linked = conn.execute(
            """
            SELECT count(*) AS n FROM app.labelled_cases
            WHERE testset_version = %s AND decision_id = %s
            """,
            (second["version"], decision_id),
        ).fetchone()["n"]
        assert linked == 0
        assert second["version"] != version

        class Guard:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, query, params=None, **kwargs):
                text = query if isinstance(query, str) else str(query)
                assert "decisions_log" not in text.lower()
                if params is None:
                    return self._inner.execute(query, **kwargs)
                return self._inner.execute(query, params, **kwargs)

        evaluate_testset(Guard(conn))
        conn.rollback()
    rejected = tmp_path / "hand_labels.json"
    rejected.write_text('{"label_origin":"agent","cases":[]}\n', encoding="utf-8")
    monkeypatch.setattr("services.testset.HAND_LABELS", rejected)
    with pytest.raises(AppError, match="hand_written"):
        load_hand_labels()
