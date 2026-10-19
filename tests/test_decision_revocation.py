"""A decided row stays out of the queue. Revocation is a new decision."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from services.companies import NO_COMPANY_UPLOAD
from services.db import connect, password_hash
from services.revocation import (
    EXECUTED_MESSAGE,
    QUEUE_MESSAGE,
    REASON_MESSAGE,
    assert_queue_transition,
)
from services.testset import inbox_decision_metric
from tests.test_flow import decide, give_company, load_demo, login

NOTICE = NO_COMPANY_UPLOAD
MOTIVE = "annullo perché la decisione era sbagliata"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _reopen(conn, statement: str, params: tuple) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="non torna in coda"):
        conn.execute(statement, params)
        conn.commit()
    conn.rollback()


def test_server_refuses_a_return_to_the_queue():
    assert_queue_transition("recommendations", "detected", "proposed")
    assert_queue_transition("field_mappings", "proposed", "proposed")
    with pytest.raises(Exception, match=QUEUE_MESSAGE):
        assert_queue_transition("recommendations", "approved", "proposed")
    with pytest.raises(Exception, match=QUEUE_MESSAGE):
        assert_queue_transition("field_mappings", "confirmed", "proposed")
    with pytest.raises(Exception, match=QUEUE_MESSAGE):
        assert_queue_transition("identity_reviews", "confirmed", "open")
    with pytest.raises(Exception, match=QUEUE_MESSAGE):
        assert_queue_transition("quarantine_documents", "released", "open")
    with pytest.raises(Exception, match=QUEUE_MESSAGE):
        assert_queue_transition("drift_reviews", "acknowledged", "open")


def test_database_and_api_keep_decided_rows_out_of_the_queue(client: TestClient):
    company_id = give_company("mapper", "Code chiuse")
    login(client, "mapper")
    review_id = str(uuid.uuid4())
    quarantine_id = str(uuid.uuid4())
    drift_id = str(uuid.uuid4())
    mapping_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app.identity_reviews (
              id, entity_type, left_key, right_key, score, reason, status
            )
            VALUES (%s, 'Customer', 'L1', 'R1', 0.72, 'Nomi vicini senza identità comune', 'open')
            """,
            (review_id,),
        )
        conn.execute(
            """
            INSERT INTO app.quarantine_documents (id, source_name, order_key, reasons, status)
            VALUES (%s, 'archivio-revoca', '2002', '[]'::jsonb, 'open')
            """,
            (quarantine_id,),
        )
        conn.execute(
            """
            INSERT INTO app.drift_reviews (id, source_name, previous_hash, new_hash, diff, status)
            VALUES (%s, 'archivio-revoca', 'prima', 'dopo', '{"added":["x"]}'::jsonb, 'open')
            """,
            (drift_id,),
        )
        conn.execute(
            """
            INSERT INTO app.field_mappings (
              id, source_name, source_field, target_type, target_property,
              confidence, status, profile, company_id
            )
            VALUES (%s, 'archivio-revoca', 'cod', 'OrderLine', 'product_source_key', 0.8, 'confirmed', 'order_line', %s)
            """,
            (mapping_id, company_id),
        )
        conn.commit()

    confirmed = client.post(f"/api/identity-reviews/{review_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    again = client.post(f"/api/identity-reviews/{review_id}/confirm")
    assert again.status_code == 409
    assert again.json()["detail"] == QUEUE_MESSAGE

    seen = client.post(f"/api/drift/{drift_id}/acknowledge")
    assert seen.status_code == 200, seen.text
    seen_again = client.post(f"/api/drift/{drift_id}/acknowledge")
    assert seen_again.status_code == 409
    assert seen_again.json()["detail"] == QUEUE_MESSAGE

    released = client.post(f"/api/quarantine/{quarantine_id}/release")
    assert released.status_code == 200, released.text

    with connect() as conn:
        _reopen(
            conn,
            "UPDATE app.identity_reviews SET status = 'open' WHERE id = %s",
            (review_id,),
        )
        _reopen(
            conn,
            "UPDATE app.drift_reviews SET status = 'open' WHERE id = %s",
            (drift_id,),
        )
        _reopen(
            conn,
            "UPDATE app.quarantine_documents SET status = 'open' WHERE id = %s",
            (quarantine_id,),
        )
        _reopen(
            conn,
            "UPDATE app.field_mappings SET status = 'proposed' WHERE id = %s",
            (mapping_id,),
        )
        stayed = conn.execute(
            """
            UPDATE app.field_mappings
            SET confidence = 0.55
            WHERE id = %s AND status = 'proposed'
            RETURNING status
            """,
            (mapping_id,),
        ).fetchone()
        assert stayed is None
        status = conn.execute(
            "SELECT status FROM app.field_mappings WHERE id = %s",
            (mapping_id,),
        ).fetchone()["status"]
        assert status == "confirmed"
        conn.commit()


def test_revocation_is_one_new_decision_with_a_reason_and_a_company(client: TestClient):
    company_id = give_company("mapper", "Annullamenti")
    login(client, "mapper")
    review_id = str(uuid.uuid4())
    quarantine_id = str(uuid.uuid4())
    drift_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app.identity_reviews (
              id, entity_type, left_key, right_key, score, reason, status
            )
            VALUES (%s, 'Customer', 'L2', 'R2', 0.72, 'Nomi vicini senza identità comune', 'open')
            """,
            (review_id,),
        )
        conn.execute(
            """
            INSERT INTO app.quarantine_documents (id, source_name, order_key, reasons, status)
            VALUES (%s, 'archivio-motivo', '2003', '[]'::jsonb, 'open')
            """,
            (quarantine_id,),
        )
        conn.execute(
            """
            INSERT INTO app.drift_reviews (id, source_name, previous_hash, new_hash, diff, status)
            VALUES (%s, 'archivio-motivo', 'a', 'b', '{"added":["y"]}'::jsonb, 'open')
            """,
            (drift_id,),
        )
        conn.commit()
    assert client.post(f"/api/identity-reviews/{review_id}/confirm").status_code == 200
    assert client.post(f"/api/quarantine/{quarantine_id}/release").status_code == 200
    assert client.post(f"/api/drift/{drift_id}/acknowledge").status_code == 200

    missing_reason = client.post(f"/api/identity-reviews/{review_id}/revoke", json={})
    assert missing_reason.status_code == 422
    short = client.post(f"/api/identity-reviews/{review_id}/revoke", json={"reason": "no"})
    assert short.status_code == 422
    assert short.json()["detail"] == REASON_MESSAGE

    revoked = client.post(
        f"/api/identity-reviews/{review_id}/revoke",
        json={"reason": MOTIVE},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["decision"] == "revoked"
    assert revoked.json()["revoked_decision_id"] == review_id
    assert revoked.json()["company_id"] == company_id
    duplicate = client.post(f"/api/identity-reviews/{review_id}/revoke", json={"reason": MOTIVE})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Decisione già annullata"

    held = client.post(f"/api/quarantine/{quarantine_id}/revoke", json={"reason": MOTIVE})
    assert held.status_code == 200, held.text
    drift = client.post(f"/api/drift/{drift_id}/revoke", json={"reason": MOTIVE})
    assert drift.status_code == 200, drift.text

    with connect() as conn:
        original = conn.execute(
            "SELECT status FROM app.identity_reviews WHERE id = %s",
            (review_id,),
        ).fetchone()["status"]
        assert original == "confirmed"
        row = conn.execute(
            """
            SELECT company_id::text AS company_id, decision, reason, actor
            FROM app.identity_review_decisions
            WHERE revoked_decision_id = %s
            """,
            (review_id,),
        ).fetchone()
        assert row["company_id"] == company_id
        assert row["decision"] == "revoked"
        assert row["reason"] == MOTIVE
        assert row["actor"] == "mapper"
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                """
                INSERT INTO app.identity_review_decisions (
                  id, company_id, decision, revoked_decision_id, reason, actor, actor_role
                )
                VALUES (%s, %s, 'revoked', %s, %s, 'mapper', 'mapper')
                """,
                (str(uuid.uuid4()), company_id, review_id, MOTIVE),
            )
            conn.commit()
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="motivo"):
            conn.execute(
                """
                INSERT INTO app.quarantine_decisions (
                  id, company_id, decision, revoked_decision_id, reason, actor, actor_role
                )
                VALUES (%s, %s, 'revoked', %s, 'no', 'mapper', 'mapper')
                """,
                (str(uuid.uuid4()), company_id, quarantine_id),
            )
            conn.commit()
        conn.rollback()

    reviews = client.get("/api/identity-reviews").json()["reviews"]
    shown = next(item for item in reviews if item["id"] == review_id)
    assert shown["status"] == "confirmed"
    assert shown["history"][0]["revoked"] is True
    assert shown["history"][0]["revoker"] == "mapper"
    assert shown["history"][0]["revoked_at"]
    assert shown["history"][0]["revoke_reason"] == MOTIVE
    quarantine = client.get("/api/quarantine").json()["documents"]
    released = next(item for item in quarantine if item["id"] == quarantine_id)
    assert released["status"] == "released"
    assert released["history"][0]["revoked"] is True
    assert released["history"][0]["revoker"] == "mapper"


def test_revocation_refuses_a_user_without_a_company_and_an_executed_action(client: TestClient):
    give_company("mapper", "Esecuzioni")
    login(client, "mapper")
    load_demo()
    login(client, "approver")
    recommendation = client.get("/api/inbox").json()["recommendations"][0]
    approved = decide(client, recommendation)
    assert approved.status_code == 200, approved.text
    inbox = client.get("/api/inbox").json()["recommendations"]
    decided = next(item for item in inbox if item["id"] == recommendation["id"])
    assert decided["executed"] is False
    decision_id = decided["history"][0]["id"]
    assert decided["history"][0]["decision"] == "approve"
    assert decided["history"][0]["revoked"] is False

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app.users (username, password_hash, role)
            VALUES ('senza-revoca', %s, 'approver')
            """,
            (password_hash("senza-revoca-secret"),),
        )
        conn.commit()
    client.post("/api/logout")
    logged = client.post(
        "/api/login",
        json={"username": "senza-revoca", "password": "senza-revoca-secret"},
    )
    assert logged.status_code == 200, logged.text
    denied = client.post(f"/api/decisions/{decision_id}/revoke", json={"reason": MOTIVE})
    assert denied.status_code == 403
    assert denied.json()["detail"] == NOTICE

    login(client, "approver")
    revoked = client.post(f"/api/decisions/{decision_id}/revoke", json={"reason": MOTIVE})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["company_id"]
    again = client.post(f"/api/decisions/{decision_id}/revoke", json={"reason": MOTIVE})
    assert again.status_code == 409
    history = next(
        item["history"][0]
        for item in client.get("/api/inbox").json()["recommendations"]
        if item["id"] == recommendation["id"]
    )
    assert history["revoked"] is True
    assert history["revoker"] == "approver"
    assert history["revoke_reason"] == MOTIVE
    assert history["revoked_at"]

    with connect() as conn:
        counts = inbox_decision_metric(conn)
    assert counts["approve"] == 0
    assert counts["revoked"] == 1

    other_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app.recommendations (
              id, order_entity_id, problem, evidence, evidence_hash, actions,
              expected_impact, risk, confidence, status, requires_cfo,
              confidence_method, confidence_rule
            )
            SELECT %s, order_entity_id, problem, evidence, evidence_hash, actions,
                   expected_impact, risk, confidence, 'proposed', requires_cfo,
                   confidence_method, confidence_rule
            FROM app.recommendations
            WHERE id = %s
            """,
            (other_id, recommendation["id"]),
        )
        conn.commit()
    other = next(
        item for item in client.get("/api/inbox").json()["recommendations"]
        if item["id"] == other_id
    )
    assert other["status"] == "proposed"
    second = decide(client, other)
    assert second.status_code == 200, second.text
    executed = client.post(f"/api/recommendations/{other['id']}/execute")
    assert executed.status_code == 200, executed.text
    blocked = client.get("/api/inbox").json()["recommendations"]
    done = next(item for item in blocked if item["id"] == other["id"])
    assert done["executed"] is True
    assert done["executed_message"] == EXECUTED_MESSAGE
    refused = client.post(
        f"/api/decisions/{done['history'][0]['id']}/revoke",
        json={"reason": MOTIVE},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == EXECUTED_MESSAGE
    with connect() as conn:
        still = conn.execute(
            "SELECT status FROM app.recommendations WHERE id = %s",
            (other["id"],),
        ).fetchone()["status"]
        assert still == "executed"
        counted = inbox_decision_metric(conn)
    assert counted["approve"] == 1
    assert counted["revoked"] == 1
