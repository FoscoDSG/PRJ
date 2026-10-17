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
    assert opened.headers["cache-control"] == "no-store"
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
    assert report["confronto"] == "agente = regole, nessun confronto possibile"
    assert set(report["groups"]) == {"mapping"}

    def four_mappings():
        return [
            {
                "case_kind": "mapping",
                "profile": "order",
                "source_field": "ord_head",
                "target_type": "Order",
                "target_property": "source_key",
                "source_name": f"fonte-{index}",
                "score": 0.98,
            }
            for index in range(4)
        ]

    monkeypatch.setattr("services.testset.load_hand_labels", four_mappings)
    with connect() as conn:
        build_testset(conn)
        partial = evaluate_testset(conn)
        conn.commit()
    assert partial["groups"]["mapping"]["method"] == "leave_one_source_out"
    assert partial["groups"]["mapping"]["separated"] is True
    assert partial["groups"]["mapping"]["n"] == 4
    assert len(partial["groups"]["mapping"]["folds"]) == 4
    for fold in partial["groups"]["mapping"]["folds"]:
        assert fold["sources"] == 1
        assert fold["cases"] == 1
        assert set(fold["test_sources"]).isdisjoint(fold["selection_source_names"])
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
                "source_name": f"fonte-{index % 5}",
                "score": 0.98,
            }
            for index in range(MIN_MEASURED_CASES)
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

        def boom(*_args, **_kwargs):
            raise AssertionError("la valutazione non ricalcola la regola")

        monkeypatch.setattr("services.testset.assigned_mapping", boom, raising=False)
        monkeypatch.setattr("services.mapping.engine.assigned_mapping", boom)
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
    assert stored["method"] == "source_kfold5"
    folds = measured["groups"]["mapping"]["folds"]
    assert folds
    assert sum(fold["cases"] for fold in folds) == MIN_MEASURED_CASES
    for fold in folds:
        assert fold["sources"] >= 1
        assert fold["cases"] >= 1
        assert set(fold["test_sources"]).isdisjoint(fold["selection_source_names"])
    assert stored["rules_version"] == HEADER_SIMILARITY_RULE
    assert float(stored["threshold"]) == pytest.approx(0.98)
    assert measured["groups"]["mapping"]["separated"] is True
    assert measured["groups"]["mapping"]["n"] == MIN_MEASURED_CASES
    assert measured["groups"]["mapping"]["human_above"] == MIN_MEASURED_CASES
    assert measured["groups"]["mapping"]["human_below"] == 0
    assert measured["confronto"] == "agente = regole, nessun confronto possibile"
    assert set(measured["groups"]) == {"mapping"}


def test_a_source_is_never_in_selection_and_evaluation():
    from services.testset import _mapping_report

    def mapping(source: str, score: float = 0.98) -> dict:
        return {
            "profile": "order",
            "source_field": "ord_head",
            "target_type": "Order",
            "target_property": "source_key",
            "source_name": source,
            "score": score,
            "confidence_rule": "mapping.header_similarity@v1",
            "correct": True,
            "decision": "confirm",
            "confidence_method": "human",
        }

    two = [mapping(source) for source in ("alpha", "beta") for _ in range(10)]
    report, threshold = _mapping_report(two)
    assert report["method"] == "leave_one_source_out"
    assert report["separated"] is True
    assert len(report["folds"]) == 2
    for fold in report["folds"]:
        assert fold["sources"] == 1
        assert fold["cases"] == 10
        assert fold["selection_sources"] == 1
        assert fold["selection_cases"] == 10
        assert set(fold["test_sources"]).isdisjoint(fold["selection_source_names"])
    assert threshold["measured"] is False

    many = [mapping(f"fonte-{index % 6}") for index in range(MIN_MEASURED_CASES)]
    report, threshold = _mapping_report(many)
    assert report["method"] == "source_kfold5"
    assert threshold["measured"] is True
    assert threshold["method"] == "source_kfold5"
    seen = []
    for fold in report["folds"]:
        assert fold["sources"] >= 1
        assert fold["cases"] >= 1
        assert set(fold["test_sources"]).isdisjoint(fold["selection_source_names"])
        seen.extend(fold["test_sources"])
    assert len(seen) == len(set(seen))
    assert sum(fold["cases"] for fold in report["folds"]) == MIN_MEASURED_CASES

    single, single_threshold = _mapping_report([mapping("solo")] * MIN_MEASURED_CASES)
    assert single["method"] == "single_source"
    assert single["separated"] is False
    assert single["folds"] == []
    assert single_threshold["measured"] is False
    assert float(single_threshold["threshold"]) == 0.75

    two_sources = [mapping("alpha" if index < 15 else "beta") for index in range(MIN_MEASURED_CASES)]
    _two_report, two_threshold = _mapping_report(two_sources)
    assert two_threshold["measured"] is False
    assert float(two_threshold["threshold"]) == 0.75

    three = [mapping(f"fonte-{index % 3}") for index in range(MIN_MEASURED_CASES)]
    three_report, three_threshold = _mapping_report(three)
    assert three_threshold["measured"] is True
    assert three_report["method"] == "leave_one_source_out"
    assert three_report["human_above"] == MIN_MEASURED_CASES
    assert three_report["human_below"] == 0

    mixed = [mapping("alpha", score=0.90), mapping("beta", score=0.40)] * 5
    mixed_report, mixed_threshold = _mapping_report(mixed)
    assert mixed_threshold["measured"] is False
    assert float(mixed_threshold["threshold"]) == 0.75
    assert mixed_report["human_above"] == 5
    assert mixed_report["human_below"] == 5


def test_human_mapping_confirmation_is_a_decision(client: TestClient):
    login(client, "mapper")
    load_demo()
    with connect() as conn:
        bulk_before = conn.execute(
            """
            SELECT count(*) AS n FROM app.mapping_decisions
            WHERE confidence_method = 'bulk_rule'
            """
        ).fetchone()["n"]
        human_before = conn.execute(
            """
            SELECT count(*) AS n FROM app.mapping_decisions
            WHERE confidence_method = 'human'
            """
        ).fetchone()["n"]
    assert bulk_before > 0
    assert human_before == 0
    mappings = client.get("/api/mappings").json()["mappings"]
    proposed = next(item for item in mappings if item["status"] == "proposed")
    confirmed = client.post(f"/api/mappings/{proposed['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    order_mapping = next(
        item for item in mappings
        if item["source_name"] == "ordini" and item["source_field"] == "ord_head"
    )
    blocked = client.post(
        f"/api/mappings/{order_mapping['id']}/confirm",
        params={"target_property_corrected": "status"},
    )
    assert blocked.status_code == 422, blocked.text
    corrected = client.post(
        f"/api/mappings/{order_mapping['id']}/reject",
        params={"target_property_corrected": "status"},
    )
    assert corrected.status_code == 200, corrected.text
    rejected = client.post(f"/api/mappings/{proposed['id']}/reject")
    assert rejected.status_code == 200, rejected.text
    with connect() as conn:
        human = conn.execute(
            """
            SELECT decision, confidence_method, actor, actor_role, decided_at,
                   source_name, source_field, target_property, target_property_corrected,
                   score, confidence_rule
            FROM app.mapping_decisions
            WHERE mapping_id = %s
            ORDER BY decided_at, id
            """,
            (proposed["id"],),
        ).fetchall()
        latest_order = conn.execute(
            """
            SELECT decision, confidence_method, target_property, target_property_corrected, score
            FROM app.mapping_decisions
            WHERE mapping_id = %s
            ORDER BY decided_at DESC, id DESC
            LIMIT 1
            """,
            (order_mapping["id"],),
        ).fetchone()
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("UPDATE app.mapping_decisions SET actor = 'altro'")
        conn.rollback()
        built = build_testset(conn)
        stored_rows = conn.execute(
            """
            SELECT label_origin, decision_id, mapping_decision_id, label
            FROM app.labelled_cases
            WHERE testset_version = %s AND case_kind = 'mapping' AND label_origin = 'human_decision'
            """,
            (built["version"],),
        ).fetchall()
        report = evaluate_testset(conn)
        conn.rollback()
    assert [row["decision"] for row in human] == ["confirm", "reject"]
    assert all(row["confidence_method"] == "human" for row in human)
    assert human[0]["actor"] == "mapper"
    assert human[0]["decided_at"] is not None
    assert human[0]["source_name"] == proposed["source_name"]
    assert human[0]["source_field"] == proposed["source_field"]
    assert human[0]["target_property"] == proposed["target_property"]
    assert float(human[0]["score"]) == pytest.approx(float(proposed["confidence"]))
    assert human[0]["confidence_rule"]
    assert human[1]["decision"] == "reject"
    assert latest_order["decision"] == "reject"
    assert latest_order["confidence_method"] == "human"
    assert latest_order["target_property_corrected"] == "status"
    assert latest_order["target_property"] != "status"
    assert len(stored_rows) == 2
    assert all(row["decision_id"] is None for row in stored_rows)
    assert all(row["mapping_decision_id"] is not None for row in stored_rows)
    labels = [row["label"] for row in stored_rows]
    assert all(label["confidence_method"] == "human" for label in labels)
    assert all(label["who"] == "mapper" and label["when"] for label in labels)
    assert all("score" in label and label["confidence_rule"] for label in labels)
    rejected_label = next(
        label for label in labels
        if label["source_name"] == proposed["source_name"]
        and label["source_field"] == proposed["source_field"]
    )
    assert rejected_label["decision"] == "reject"
    assert rejected_label["correct"] is False
    order_label = next(label for label in labels if label["source_field"] == "ord_head")
    assert order_label["decision"] == "reject"
    assert order_label["correct"] is False
    assert order_label["target_property_corrected"] == "status"
    assert report["groups"]["mapping"]["bulk_rule"] == bulk_before - 1
    assert report["groups"]["mapping"]["missing_source"] == 0


def test_new_rows_require_the_snapshot_and_a_reject_is_an_error():
    script = (ROOT / "db" / "migrations" / "0009_mapping_decision_snapshot_checks.sql").read_text(
        encoding="utf-8"
    )
    assert "NOT VALID" in script
    assert "VALIDATE CONSTRAINT" not in script
    with connect() as conn:
        checks = conn.execute(
            """
            SELECT conname, convalidated
            FROM pg_constraint
            WHERE conrelid = 'app.mapping_decisions'::regclass
              AND conname IN (
                'mapping_decisions_snapshot_present_check',
                'mapping_decisions_correction_on_reject_check'
              )
            ORDER BY conname
            """
        ).fetchall()
        assert [row["conname"] for row in checks] == [
            "mapping_decisions_correction_on_reject_check",
            "mapping_decisions_snapshot_present_check",
        ]
        assert all(row["convalidated"] is False for row in checks)
        mapping_id = conn.execute(
            """
            INSERT INTO app.field_mappings (
              id, source_name, source_field, target_type, target_property,
              confidence, status, profile
            )
            VALUES (
              gen_random_uuid(), 'alfa', 'ord_head', 'Order', 'source_key',
              0.91, 'proposed', 'order'
            )
            RETURNING id
            """
        ).fetchone()["id"]
        conn.commit()

        def insert(target=mapping_id, **overrides):
            payload = {
                "decision": "confirm",
                "confidence_method": "human",
                "source_name": "alfa",
                "source_field": "ord_head",
                "target_property": "source_key",
                "target_property_corrected": None,
                "score": 0.91,
                "confidence_rule": "mapping.header_similarity@v1",
                "decided_at": "2020-01-01T00:00:00Z",
            }
            payload.update(overrides)
            conn.execute(
                """
                INSERT INTO app.mapping_decisions (
                  id, mapping_id, decision, confidence_method, actor, actor_role,
                  decided_at, source_name, source_field, target_property,
                  target_property_corrected, score, confidence_rule
                )
                VALUES (
                  gen_random_uuid(), %s, %s, %s, 'mapper', 'mapper',
                  %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    target,
                    payload["decision"],
                    payload["confidence_method"],
                    payload["decided_at"],
                    payload["source_name"],
                    payload["source_field"],
                    payload["target_property"],
                    payload["target_property_corrected"],
                    payload["score"],
                    payload["confidence_rule"],
                ),
            )

        for field in ("source_name", "source_field", "target_property", "score", "confidence_rule"):
            with pytest.raises(psycopg.errors.CheckViolation, match="snapshot_present"):
                insert(**{field: None})
            conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation, match="correction_on_reject"):
            insert(decision="confirm", target_property_corrected="status")
        conn.rollback()
        insert()
        insert(
            decision="reject",
            target_property_corrected="status",
            source_name="alfa",
            decided_at="2020-01-02T00:00:00Z",
        )
        kept_id = conn.execute(
            """
            INSERT INTO app.field_mappings (
              id, source_name, source_field, target_type, target_property,
              confidence, status, profile
            )
            VALUES (
              gen_random_uuid(), 'gamma', 'importo', 'Order', 'amount',
              0.80, 'proposed', 'order'
            )
            RETURNING id
            """
        ).fetchone()["id"]
        insert(
            target=kept_id,
            source_name="gamma",
            source_field="importo",
            target_property="amount",
            score=0.80,
            decided_at="2020-01-03T00:00:00Z",
        )
        other_id = conn.execute(
            """
            INSERT INTO app.field_mappings (
              id, source_name, source_field, target_type, target_property,
              confidence, status, profile
            )
            VALUES (
              gen_random_uuid(), 'beta', 'stato', 'Order', 'status',
              0.40, 'proposed', 'order'
            )
            RETURNING id
            """
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO app.mapping_decisions (
              id, mapping_id, decision, confidence_method, actor, actor_role,
              source_name, source_field, target_property, score, confidence_rule
            )
            VALUES (
              gen_random_uuid(), %s, 'confirm', 'bulk_rule', 'mapper', 'mapper',
              'beta', 'stato', 'status', 0.40, 'mapping.header_similarity@v1'
            )
            """,
            (other_id,),
        )
        built = build_testset(conn)
        stored = conn.execute(
            """
            SELECT label
            FROM app.labelled_cases
            WHERE testset_version = %s AND case_kind = 'mapping' AND label_origin = 'human_decision'
            """,
            (built["version"],),
        ).fetchall()
        conn.rollback()
    labels = [row["label"] for row in stored]
    reject = next(label for label in labels if label["decision"] == "reject")
    assert reject["correct"] is False
    assert reject["target_property_corrected"] == "status"
    assert reject["score"] == pytest.approx(0.91)
    assert any(label["decision"] == "confirm" and label["correct"] is True for label in labels)
    assert all(label["confidence_method"] == "human" for label in labels)
    assert len(labels) == 2


def test_a_missing_score_is_not_recomputed(monkeypatch: pytest.MonkeyPatch):
    from services.errors import AppError
    from services.mapping.engine import assigned_mapping

    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        return assigned_mapping(*args, **kwargs)

    monkeypatch.setattr("services.mapping.engine.assigned_mapping", spy)
    monkeypatch.setattr("services.testset.assigned_mapping", spy, raising=False)

    def one():
        return [
            {
                "case_kind": "mapping",
                "profile": "order",
                "source_field": "ord_head",
                "target_type": "Order",
                "target_property": "source_key",
                "source_name": "alfa",
            }
        ]

    monkeypatch.setattr("services.testset.load_hand_labels", one)
    with connect() as conn:
        version = build_testset(conn)["version"]
        stored = conn.execute(
            """
            SELECT label FROM app.labelled_cases
            WHERE testset_version = %s AND case_kind = 'mapping'
            """,
            (version,),
        ).fetchone()["label"]
        with pytest.raises(AppError, match="punteggio fotografato"):
            evaluate_testset(conn)
        conn.rollback()
    assert "score" not in stored
    assert calls == []


def test_empty_source_is_counted_apart(monkeypatch: pytest.MonkeyPatch):
    def labels():
        rows = []
        for source in ("alpha", "beta", None, "  "):
            item = {
                "case_kind": "mapping",
                "profile": "order",
                "source_field": "ord_head",
                "target_type": "Order",
                "target_property": "source_key",
                "score": 0.98,
            }
            if source is not None:
                item["source_name"] = source
            rows.append(item)
        return rows

    monkeypatch.setattr("services.testset.load_hand_labels", labels)
    with connect() as conn:
        build_testset(conn)
        report = evaluate_testset(conn)
        conn.rollback()
    assert report["groups"]["mapping"]["missing_source"] == 2
    assert report["groups"]["mapping"]["n"] == 2
    assert report["groups"]["mapping"]["bulk_rule"] == 0


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


def test_ieol_app_cannot_hmac_with_a_chosen_key():
    import hashlib
    import hmac

    with app_connect() as conn:
        with pytest.raises(psycopg.Error):
            conn.execute("SELECT app.hmac_evidence(ARRAY[]::uuid[], 'chosen-key')")
        conn.rollback()
        for statement in (
            "SELECT secret FROM app.evidence_hmac_key",
            "UPDATE app.evidence_hmac_key SET secret = 'chosen-key'",
            "INSERT INTO app.evidence_hmac_key (id, secret) VALUES (1, 'chosen-key')",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()
        digest = conn.execute(
            "SELECT app.hmac_evidence(ARRAY[]::uuid[]) AS digest"
        ).fetchone()["digest"]
        conn.rollback()
    chosen = hmac.new(b"chosen-key", b"", hashlib.sha256).hexdigest()
    stored = hmac.new(b"test-hmac-key-not-for-production", b"", hashlib.sha256).hexdigest()
    assert digest == stored
    assert digest != chosen


def test_cleartext_reads_go_through_one_audited_function():
    allowed = {
        ROOT / "services" / "db.py",
        ROOT / "services" / "pii.py",
    }
    for folder in (ROOT / "services", ROOT / "apps"):
        for path in folder.rglob("*.py"):
            if "pii_connect" in path.read_text(encoding="utf-8"):
                assert path in allowed, path
    source = (ROOT / "services" / "pii.py").read_text(encoding="utf-8")
    assert source.count("pii_connect(") == 1
    assert "def read_cleartext" in source


def test_pii_reads_are_audited_without_values(client: TestClient):
    login(client, "mapper")
    load_demo()
    with connect() as conn:
        rows = conn.execute(
            "SELECT action, payload FROM app.audit_log WHERE action = 'pii_read'"
        ).fetchall()
    assert rows
    for row in rows:
        _clean(row["payload"])
        assert row["payload"]["purpose"]
        assert "value_cleartext" not in row["payload"]
        assert "data_cleartext" not in row["payload"]


def test_shield_records_how_many_rows_were_masked():
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM app.audit_log WHERE action = 'pii_mask'"
        ).fetchone()["n"] == 0
        conn.execute(
            """
            INSERT INTO app.quality_issues (id, entity_type, business_key, check_name, detail)
            VALUES (gen_random_uuid(), 'Customer', 'C1', 'contatto', 'scrivere a segreteria@example.com')
            """
        )
        conn.execute("SELECT app.shield_existing_pii()")
        row = conn.execute(
            """
            SELECT payload FROM app.audit_log
            WHERE action = 'pii_mask'
            ORDER BY at DESC
            LIMIT 1
            """
        ).fetchone()
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("UPDATE app.audit_log SET actor = 'x'")
        conn.rollback()
    payload = row["payload"]
    assert payload["migration"] == "0005"
    assert payload["hash_version"] == "hmac-sha256-v1"
    assert payload["quality_issues"] >= 1
    assert "segreteria@example.com" not in json.dumps(payload)


def test_hash_version_old_rows_stay_sha256_and_new_rows_use_hmac(client: TestClient):
    script = (ROOT / "db" / "migrations" / "0005_pii_testset_audit.sql").read_text(encoding="utf-8")
    added = script.index("ADD COLUMN IF NOT EXISTS hash_version text NOT NULL DEFAULT 'sha256'")
    changed = script.index("ALTER COLUMN hash_version SET DEFAULT 'hmac-sha256-v1'")
    assert added < changed
    assert "SET hash_version" not in script
    login(client, "mapper")
    load_demo()
    login(client, "approver")
    item = client.get("/api/inbox").json()["recommendations"][0]
    assert decide(client, item).status_code == 200
    with connect() as conn:
        default = conn.execute(
            """
            SELECT column_default
            FROM information_schema.columns
            WHERE table_schema = 'app' AND table_name = 'decisions_log' AND column_name = 'hash_version'
            """
        ).fetchone()["column_default"]
        fresh = conn.execute(
            """
            SELECT hash_version FROM app.decisions_log
            WHERE actor = 'approver'
            ORDER BY decided_at DESC
            LIMIT 1
            """
        ).fetchone()["hash_version"]
        conn.execute(
            """
            INSERT INTO app.decisions_log (
              id, recommendation_id, batch_id, row_index, decision, evidence_hash,
              recommendation, confidence, actor, actor_role, agent_version, rules_version,
              confidence_threshold, amount_threshold_eur, hash_version
            )
            SELECT gen_random_uuid(), r.id, lr.batch_id, lr.row_index, 'reject', r.evidence_hash,
                   'storico', 0.2, 'archivio', 'approver', 'x', 'y', 0.75, 1, 'sha256'
            FROM app.recommendations r
            JOIN app.landing_rows lr ON lr.data ->> 'ord_head' = '1024'
            LIMIT 1
            """
        )
        historical = conn.execute(
            "SELECT hash_version FROM app.decisions_log WHERE actor = 'archivio'"
        ).fetchone()["hash_version"]
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(
                "UPDATE app.decisions_log SET hash_version = 'hmac-sha256-v1' WHERE actor = 'archivio'"
            )
        conn.rollback()
    assert "hmac-sha256-v1" in default
    assert fresh == "hmac-sha256-v1"
    assert historical == "sha256"


def test_hash_version_v2_is_allowed_and_is_not_the_default(client: TestClient):
    script = (ROOT / "db" / "migrations" / "0006_hash_version_v2.sql").read_text(encoding="utf-8")
    assert "hmac-sha256-v2" in script
    assert "SET DEFAULT" not in script
    assert "UPDATE " not in script
    engine = (ROOT / "services" / "workflow" / "engine.py").read_text(encoding="utf-8")
    assert '"hmac-sha256-v1"' in engine
    assert "hmac-sha256-v2" not in engine
    login(client, "mapper")
    load_demo()
    with connect() as conn:
        default = conn.execute(
            """
            SELECT column_default
            FROM information_schema.columns
            WHERE table_schema = 'app' AND table_name = 'decisions_log' AND column_name = 'hash_version'
            """
        ).fetchone()["column_default"]
        check = conn.execute(
            """
            SELECT pg_get_constraintdef(oid) AS definition
            FROM pg_constraint
            WHERE conname = 'decisions_log_hash_version_check'
            """
        ).fetchone()["definition"]
        inserted = conn.execute(
            """
            INSERT INTO app.decisions_log (
              id, recommendation_id, batch_id, row_index, decision, evidence_hash,
              recommendation, confidence, actor, actor_role, agent_version, rules_version,
              confidence_threshold, amount_threshold_eur, hash_version
            )
            SELECT gen_random_uuid(), r.id, lr.batch_id, lr.row_index, 'reject', r.evidence_hash,
                   'futuro', 0.2, 'rotazione', 'approver', 'x', 'y', 0.75, 1, 'hmac-sha256-v2'
            FROM app.recommendations r
            JOIN app.landing_rows lr ON lr.data ->> 'ord_head' = '1024'
            LIMIT 1
            RETURNING hash_version
            """
        ).fetchone()["hash_version"]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO app.decisions_log (
                  id, recommendation_id, batch_id, row_index, decision, evidence_hash,
                  recommendation, confidence, actor, actor_role, agent_version, rules_version,
                  confidence_threshold, amount_threshold_eur, hash_version
                )
                SELECT gen_random_uuid(), r.id, lr.batch_id, lr.row_index, 'reject', r.evidence_hash,
                       'futuro', 0.2, 'rifiutata', 'approver', 'x', 'y', 0.75, 1, 'hmac-sha256-v3'
                FROM app.recommendations r
                JOIN app.landing_rows lr ON lr.data ->> 'ord_head' = '1024'
                LIMIT 1
                """
            )
        conn.rollback()
    assert "hmac-sha256-v1" in default
    assert "hmac-sha256-v2" not in default
    assert "hmac-sha256-v2" in check
    assert "hmac-sha256-v3" not in check
    assert inserted == "hmac-sha256-v2"


def test_bad_date_and_entity_id_are_422(client: TestClient):
    login(client, "admin")
    bad_date = client.get("/api/audit/readable", params={"date_from": "non-una-data"})
    assert bad_date.status_code == 422
    bad_end = client.get("/api/audit/readable", params={"date_to": "2026-13-40"})
    assert bad_end.status_code == 422
    bad_id = client.post(
        "/api/pii/unmask",
        json={"entity_id": "non-e-uno-uuid", "reason": "motivo valido"},
    )
    assert bad_id.status_code == 422


def test_detail_keys_use_the_italian_labels():
    from services.audit_read import _detail

    text = _detail(
        {
            "decision": "approve",
            "status": "open",
            "quantity": 3,
            "reason": "controllo",
            "problem": "ritardo",
        }
    )
    assert text == "Decisione: Approvazione; Stato: Aperto; Quantità: 3; Motivo: controllo; Problema: ritardo"
    assert "decision:" not in text
    assert "quantity:" not in text


def test_status_values_use_the_italian_labels():
    from services.audit_read import _detail

    expected = {
        "open": "Aperto",
        "acknowledged": "Visto",
        "detected": "Rilevato",
        "proposed": "Proposto",
        "confirmed": "Confermato",
        "approved": "Approvato",
        "modified": "Modificato",
        "rejected": "Rifiutato",
        "released": "Rilasciato",
        "dry_run_ok": "Simulazione riuscita",
        "executed": "Eseguito",
        "failed": "Fallito",
        "expired": "Scaduto",
        "queued": "In coda",
        "compensated": "Compensato",
    }
    for code, label in expected.items():
        assert _detail({"status": code}) == f"Stato: {label}"


def test_unmask_field_and_measured_threshold_phrase():
    import subprocess

    script = r"""
const { customerDetail, confidenceLabel } = require("./apps/web/escape.js");
const fs = require("fs");
const app = fs.readFileSync("./apps/web/app.js", "utf8");
if (app.includes("window.prompt")) process.exit(2);
const html = customerDetail({
  id: "11111111-1111-1111-1111-111111111111",
  company_name: "x",
  orders: [],
  edges: [],
  facts: [],
}, "admin");
if (!html.includes("data-reason") || !html.includes("unmask-pii")) process.exit(3);
if (html.includes("window.prompt")) process.exit(4);
const plain = confidenceLabel({
  confidence: 0.93,
  confidence_method: "rule",
  confidence_rule: "mapping.header_similarity@v1",
  calibrated: false,
});
if (plain !== "0,93 · punteggio di regola, non calibrato · mapping.header_similarity@v1") process.exit(8);
const measured = confidenceLabel({
  confidence: 0.93,
  confidence_method: "rule",
  calibrated: true,
  measured: true,
  support: 30,
});
if (measured !== "soglia misurata su 30 casi" || measured.includes("%")) process.exit(9);
process.stdout.write("ok\n");
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout


def test_calibrated_stays_false_when_threshold_is_measured(client: TestClient):
    login(client, "mapper")
    load_demo()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app.mapping_thresholds (
              id, threshold, method, measured, rules_version, metrics, support
            )
            VALUES (gen_random_uuid(), 0.91, 'kfold5_f1_v1', true, 'mapping.header_similarity@v1', '{}', 30)
            """
        )
        conn.commit()
    mappings = client.get("/api/mappings").json()["mappings"]
    assert mappings
    assert all(item["calibrated"] is False for item in mappings)
    assert all(item["measured"] is True and item["support"] == 30 for item in mappings)
    order = next(item for item in client.get("/api/orders").json()["orders"] if item["business_key"] == "1024")
    facts = client.get(f"/api/orders/{order['id']}").json()["facts"]
    assert facts
    assert all(fact["calibrated"] is False for fact in facts)
    assert all(fact["measured"] is True and fact["support"] == 30 for fact in facts)


def test_synthetic_cases_do_not_choose_the_threshold(monkeypatch: pytest.MonkeyPatch):
    from services.agent.evaluate import load_cases

    labels = json.loads((ROOT / "evals" / "hand_labels.json").read_text(encoding="utf-8"))
    orders = json.loads((ROOT / "evals" / "order_cases.json").read_text(encoding="utf-8"))
    assert labels["label_origin"] == "synthetic"
    assert orders["label_origin"] == "synthetic"
    assert load_cases()[0]["id"] == "shortage-fast-supplier"

    def many_synthetic():
        return [
            {
                "case_kind": "mapping",
                "label_origin": "synthetic",
                "profile": "order",
                "source_field": "ord_head",
                "target_type": "Order",
                "target_property": "source_key",
            }
            for _ in range(MIN_MEASURED_CASES)
        ]

    monkeypatch.setattr("services.testset.load_hand_labels", many_synthetic)
    with connect() as conn:
        built = build_testset(conn)
        report = evaluate_testset(conn)
        stored = conn.execute(
            """
            SELECT count(*) AS n FROM app.labelled_cases
            WHERE label_origin = 'synthetic' AND testset_version = %s
            """,
            (built["version"],),
        ).fetchone()["n"]
        conn.commit()
    assert stored == MIN_MEASURED_CASES
    assert built["mapping_count"] == MIN_MEASURED_CASES
    assert report["threshold"]["measured"] is False
    assert float(report["threshold"]["threshold"]) == 0.75
    assert report["groups"]["mapping"]["n"] == 0
    assert report["confronto"] == "agente = regole, nessun confronto possibile"
