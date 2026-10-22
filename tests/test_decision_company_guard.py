"""Decisions need a company: the page, the API, and new database rows."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from services.companies import USER_WITHOUT_COMPANY
from services.pipeline import ORPHAN_STOCK
from services.db import connect, password_hash
from services.mapping.corrections import learned_metric
from services.migrate import _ensure_registry, _execute_migration, _record, migrate, migration_files
from services.testset import _human_mapping_cases, build_testset, evaluate_testset
from tests.helpers import wipe_test_database
from tests.test_flow import decide, give_company, load_demo, login
from tests.test_learning import _batch, _decision, _mapping, _user

ROOT = Path(__file__).resolve().parents[1]
NOTICE = USER_WITHOUT_COMPANY


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _measure(conn) -> dict:
    """First-impact recognition accuracy and the learned-corrections count.

    Both numbers come from the existing selectors. This test does not
    change those functions.
    """
    recognition_ids = [item["decision_id"] for item in _human_mapping_cases(conn)]
    learned = learned_metric(conn)
    build_testset(conn)
    report = evaluate_testset(conn)["groups"]["mapping"]
    return {
        "recognition_ids": recognition_ids,
        "accuracy": report["accuracy"],
        "n": report["n"],
        "errors": report["errors"],
        "learned": learned,
    }


def _prepare_through_0011(conn) -> None:
    wipe_test_database(conn)
    _ensure_registry(conn)
    for item in migration_files():
        if item.version >= "0012":
            continue
        _execute_migration(conn, item)
        _record(conn, item, baselined=False)
    conn.commit()


def test_0012_does_not_rewrite_rows_and_keeps_the_two_measures():
    script = (ROOT / "db" / "migrations" / "0012_decision_company_guard.sql").read_text(encoding="utf-8")
    code = "\n".join(line.split("--", 1)[0] for line in script.splitlines())
    assert "UPDATE app." not in code
    assert "UPDATE src." not in code
    assert "NOT VALID" in script
    later = (ROOT / "db" / "migrations" / "0013_decided_row_revocation.sql").read_text(encoding="utf-8")
    later_code = "\n".join(line.split("--", 1)[0] for line in later.splitlines())
    assert "UPDATE app." not in later_code
    assert "UPDATE src." not in later_code
    guard = (ROOT / "db" / "migrations" / "0014_revoked_outcome_guard.sql").read_text(encoding="utf-8")
    guard_code = "\n".join(line.split("--", 1)[0] for line in guard.splitlines())
    assert "UPDATE app." not in guard_code
    assert "UPDATE src." not in guard_code

    company_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    with connect() as conn:
        _prepare_through_0011(conn)
        conn.execute(
            "INSERT INTO app.companies (id, name, is_demo) VALUES (%s, 'Alfa', false), (%s, 'Beta', false)",
            (company_id, other_id),
        )
        _user(conn, "ada", company_id)
        _user(conn, "bea", other_id)
        conn.execute(
            """
            INSERT INTO app.users (username, password_hash, role, company_id)
            VALUES ('mapper', 'not-a-password', 'mapper', NULL)
            """
        )
        first_alfa = _batch(conn, company_id, "alfa", "a1", loaded_at="2024-01-01T00:00:00Z")
        later_alfa = _batch(conn, company_id, "alfa", "a2", loaded_at="2024-08-01T00:00:00Z")
        first_beta = _batch(conn, company_id, "beta", "b1", loaded_at="2024-01-01T00:00:00Z")
        null_batch = _batch(conn, None, "vecchia", "v1", loaded_at="2019-01-01T00:00:00Z")
        alfa_map = _mapping(
            conn, company_id=company_id, source="alfa", field="cod_art", batch_id=first_alfa,
        )
        later_map = _mapping(
            conn, company_id=company_id, source="alfa", field="qty", batch_id=later_alfa,
        )
        beta_map = _mapping(
            conn, company_id=company_id, source="beta", field="cod_art", batch_id=first_beta,
        )
        learned_ok = _mapping(conn, company_id=company_id, source="delta", field="COD_ART")
        learned_no = _mapping(conn, company_id=company_id, source="delta", field="QTY")
        null_map = _mapping(
            conn, company_id=None, source="vecchia", field="cod_art", batch_id=null_batch,
        )
        kept = _decision(
            conn, alfa_map, decision="confirm", company_id=company_id, actor="ada",
            source_name="alfa", source_field="cod_art", origin="rule", batch_id=first_alfa,
            decided_at="2024-02-01T00:00:00Z",
        )
        _decision(
            conn, later_map, decision="confirm", company_id=company_id, actor="ada",
            source_name="alfa", source_field="qty", origin="rule", batch_id=later_alfa,
            decided_at="2024-09-01T00:00:00Z",
        )
        other = _decision(
            conn, beta_map, decision="confirm", company_id=company_id, actor="ada",
            source_name="beta", source_field="cod_art", origin="rule", batch_id=first_beta,
            decided_at="2024-02-01T00:00:00Z",
        )
        learned_from_ok = _decision(
            conn, learned_ok, decision="confirm", company_id=company_id, actor="ada",
            source_name="delta", source_field="COD_ART", origin="rule",
            decided_at="2024-02-15T00:00:00Z",
        )
        learned_from_no = _decision(
            conn, learned_no, decision="confirm", company_id=company_id, actor="ada",
            source_name="delta", source_field="QTY", origin="rule",
            decided_at="2024-02-15T00:00:00Z",
        )
        _decision(
            conn, learned_ok, decision="confirm", company_id=company_id, actor="ada",
            source_name="delta", source_field="COD_ART", origin="learned",
            source_decision_id=learned_from_ok, decided_at="2024-03-01T00:00:00Z",
        )
        _decision(
            conn, learned_no, decision="reject", company_id=company_id, actor="ada",
            source_name="delta", source_field="QTY", origin="learned",
            source_decision_id=learned_from_no, decided_at="2024-03-01T00:00:00Z",
        )
        historical = _decision(
            conn, null_map, decision="confirm", company_id=None, actor="mapper",
            source_name="vecchia", source_field="cod_art", origin="rule", batch_id=null_batch,
            decided_at="2019-02-01T00:00:00Z",
        )
        review_id = str(uuid.uuid4())
        quarantine_id = str(uuid.uuid4())
        drift_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO app.identity_reviews (
              id, entity_type, left_key, right_key, score, reason, status
            )
            VALUES (%s, 'Customer', 'C1', 'C2', 0.72, 'Nomi vicini senza identità comune', 'confirmed')
            """,
            (review_id,),
        )
        conn.execute(
            """
            INSERT INTO app.quarantine_documents (id, source_name, order_key, reasons, status)
            VALUES (%s, 'archivio', '1001', '[]'::jsonb, 'released')
            """,
            (quarantine_id,),
        )
        conn.execute(
            """
            INSERT INTO app.drift_reviews (id, source_name, previous_hash, new_hash, diff, status)
            VALUES (%s, 'archivio', 'old', 'new', '{"added":["x"]}'::jsonb, 'acknowledged')
            """,
            (drift_id,),
        )
        conn.commit()
        before = _measure(conn)
        before_rows = {
            "mapping": conn.execute(
                """
                SELECT id::text AS id, company_id::text AS company_id, decision
                FROM app.mapping_decisions WHERE id = %s
                """,
                (historical,),
            ).fetchone(),
            "review": conn.execute(
                "SELECT id::text AS id, status FROM app.identity_reviews WHERE id = %s",
                (review_id,),
            ).fetchone(),
            "quarantine": conn.execute(
                "SELECT id::text AS id, status FROM app.quarantine_documents WHERE id = %s",
                (quarantine_id,),
            ).fetchone(),
            "drift": conn.execute(
                "SELECT id::text AS id, status FROM app.drift_reviews WHERE id = %s",
                (drift_id,),
            ).fetchone(),
        }
        conn.commit()
        applied = []
        for version in ("0012", "0013", "0014"):
            item = next(found for found in migration_files() if found.version == version)
            _execute_migration(conn, item)
            _record(conn, item, baselined=False)
            conn.commit()
            applied.append({"version": item.version, "name": item.name, "baselined": False})
            if version == "0012":
                after_guard = _measure(conn)
        after = _measure(conn)
        historical_company = conn.execute(
            "SELECT company_id::text AS company_id FROM app.mapping_decisions WHERE id = %s",
            (historical,),
        ).fetchone()["company_id"]
        review = conn.execute(
            """
            SELECT status, company_id::text AS company_id, actor
            FROM app.identity_reviews WHERE id = %s
            """,
            (review_id,),
        ).fetchone()
        quarantine = conn.execute(
            """
            SELECT status, company_id::text AS company_id, actor
            FROM app.quarantine_documents WHERE id = %s
            """,
            (quarantine_id,),
        ).fetchone()
        drift = conn.execute(
            """
            SELECT status, company_id::text AS company_id, actor
            FROM app.drift_reviews WHERE id = %s
            """,
            (drift_id,),
        ).fetchone()
        conn.commit()

    assert applied == [
        {"version": "0012", "name": "decision_company_guard", "baselined": False},
        {"version": "0013", "name": "decided_row_revocation", "baselined": False},
        {"version": "0014", "name": "revoked_outcome_guard", "baselined": False},
    ]
    assert before["accuracy"] == after_guard["accuracy"] == after["accuracy"]
    assert before["n"] == after_guard["n"] == after["n"]
    assert before["errors"] == after_guard["errors"] == after["errors"]
    assert before["learned"] == after_guard["learned"] == after["learned"]
    assert before["recognition_ids"] == after_guard["recognition_ids"] == after["recognition_ids"]
    assert before["accuracy"] == 1.0
    assert before["n"] == 2
    assert set(before["recognition_ids"]) == {kept, other}
    assert historical not in before["recognition_ids"]
    assert before["learned"] == {"confirmed": 1, "rejected": 1, "revoked": {"rule": 0, "learned": 0}}
    assert before_rows["mapping"]["company_id"] is None
    assert historical_company is None
    assert review["status"] == "confirmed" and review["company_id"] is None and review["actor"] is None
    assert quarantine["status"] == "released" and quarantine["company_id"] is None
    assert drift["status"] == "acknowledged" and drift["company_id"] is None and drift["actor"] is None


def test_database_refuses_a_new_decision_without_the_actor_company():
    company_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            "INSERT INTO app.companies (id, name) VALUES (%s, 'Alfa'), (%s, 'Beta')",
            (company_id, other_id),
        )
        _user(conn, "ada", company_id)
        _user(conn, "bea", other_id)
        host = _mapping(conn, company_id=company_id, source="isolata", field="COD_ART")
        review_id = str(uuid.uuid4())
        quarantine_id = str(uuid.uuid4())
        drift_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO app.identity_reviews (
              id, entity_type, left_key, right_key, score, reason, status
            )
            VALUES (%s, 'Customer', 'L', 'R', 0.72, 'Nomi vicini senza identità comune', 'open')
            """,
            (review_id,),
        )
        conn.execute(
            """
            INSERT INTO app.quarantine_documents (id, source_name, order_key, reasons, status)
            VALUES (%s, 'mail', '9', '[]'::jsonb, 'open')
            """,
            (quarantine_id,),
        )
        conn.execute(
            """
            INSERT INTO app.drift_reviews (id, source_name, previous_hash, new_hash, diff, status)
            VALUES (%s, 'mail', 'a', 'b', '{"added":["z"]}'::jsonb, 'open')
            """,
            (drift_id,),
        )
        conn.commit()

        with pytest.raises(psycopg.errors.RaiseException, match="decisione senza azienda"):
            with conn.transaction():
                _decision(
                    conn, host, decision="confirm", company_id=None, actor="ada",
                    source_name="isolata", source_field="COD_ART",
                )
        with pytest.raises(psycopg.errors.RaiseException, match="altra azienda"):
            with conn.transaction():
                _decision(
                    conn, host, decision="confirm", company_id=other_id, actor="ada",
                    source_name="isolata", source_field="COD_ART",
                )
        written = _decision(
            conn, host, decision="confirm", company_id=company_id, actor="ada",
            source_name="isolata", source_field="COD_ART",
        )
        conn.commit()

        for statement, params in (
            (
                """
                UPDATE app.identity_reviews
                SET status = 'confirmed', actor = 'ada'
                WHERE id = %s
                """,
                (review_id,),
            ),
            (
                """
                UPDATE app.quarantine_documents
                SET status = 'released', actor = 'ada'
                WHERE id = %s
                """,
                (quarantine_id,),
            ),
            (
                """
                UPDATE app.drift_reviews
                SET status = 'acknowledged', actor = 'ada'
                WHERE id = %s
                """,
                (drift_id,),
            ),
        ):
            with pytest.raises(psycopg.errors.RaiseException, match="decisione senza azienda"):
                with conn.transaction():
                    conn.execute(statement, params)

        with pytest.raises(psycopg.errors.RaiseException, match="altra azienda"):
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE app.identity_reviews
                    SET status = 'rejected', company_id = %s, actor = 'ada'
                    WHERE id = %s
                    """,
                    (other_id, review_id),
                )
        conn.execute(
            """
            UPDATE app.identity_reviews
            SET status = 'confirmed', company_id = %s, actor = 'ada'
            WHERE id = %s
            """,
            (company_id, review_id),
        )
        conn.execute(
            """
            UPDATE app.quarantine_documents
            SET status = 'released', company_id = %s, actor = 'ada'
            WHERE id = %s
            """,
            (company_id, quarantine_id),
        )
        conn.execute(
            """
            UPDATE app.drift_reviews
            SET status = 'acknowledged', company_id = %s, actor = 'ada'
            WHERE id = %s
            """,
            (company_id, drift_id),
        )
        conn.commit()
        stored = conn.execute(
            "SELECT company_id::text AS company_id FROM app.mapping_decisions WHERE id = %s",
            (written,),
        ).fetchone()
        statuses = {
            "review": conn.execute(
                "SELECT status, company_id::text AS company_id FROM app.identity_reviews WHERE id = %s",
                (review_id,),
            ).fetchone(),
            "quarantine": conn.execute(
                "SELECT status, company_id::text AS company_id FROM app.quarantine_documents WHERE id = %s",
                (quarantine_id,),
            ).fetchone(),
            "drift": conn.execute(
                "SELECT status, company_id::text AS company_id FROM app.drift_reviews WHERE id = %s",
                (drift_id,),
            ).fetchone(),
        }
    entity_id = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    recommendation_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            "INSERT INTO app.entities (id, type, business_key) VALUES (%s, 'Order', 'ORD-LOG')",
            (entity_id,),
        )
        conn.execute(
            """
            INSERT INTO app.landing_batches (id, source_name, watermark, row_count)
            VALUES (%s, 'inbox', 'w-log', 1)
            """,
            (batch_id,),
        )
        conn.execute(
            "INSERT INTO app.landing_rows (batch_id, row_index, data) VALUES (%s, 0, '{}'::jsonb)",
            (batch_id,),
        )
        conn.execute(
            """
            INSERT INTO app.recommendations (
              id, order_entity_id, problem, evidence, evidence_hash, actions,
              expected_impact, risk, confidence, status, requires_cfo
            )
            VALUES (%s, %s, 'manca', '[]'::jsonb, 'abc', '[]'::jsonb, 'niente', 'low', 0.5, 'proposed', false)
            """,
            (recommendation_id, entity_id),
        )
        log_sql = """
            INSERT INTO app.decisions_log (
              id, recommendation_id, batch_id, row_index, decision, evidence_hash,
              recommendation, confidence, actor, actor_role, agent_version, rules_version,
              confidence_threshold, amount_threshold_eur, company_id
            )
            VALUES (%s, %s, %s, 0, 'approve', 'abc', 'manca', 0.5, %s, 'approver', 'v', 'v', 0.75, 1, %s)
        """
        with pytest.raises(psycopg.errors.RaiseException, match="decisione senza azienda"):
            with conn.transaction():
                conn.execute(
                    log_sql,
                    (str(uuid.uuid4()), recommendation_id, batch_id, "ada", None),
                )
        with pytest.raises(psycopg.errors.RaiseException, match="altra azienda"):
            with conn.transaction():
                conn.execute(
                    log_sql,
                    (str(uuid.uuid4()), recommendation_id, batch_id, "ada", other_id),
                )
        written_log = str(uuid.uuid4())
        conn.execute(
            log_sql,
            (written_log, recommendation_id, batch_id, "ada", company_id),
        )
        conn.commit()
        logged_company = conn.execute(
            "SELECT company_id::text AS company_id FROM app.decisions_log WHERE id = %s",
            (written_log,),
        ).fetchone()["company_id"]
    assert logged_company == company_id

    assert stored["company_id"] == company_id
    assert statuses["review"]["status"] == "confirmed"
    assert statuses["review"]["company_id"] == company_id
    assert statuses["quarantine"]["status"] == "released"
    assert statuses["quarantine"]["company_id"] == company_id
    assert statuses["drift"]["status"] == "acknowledged"
    assert statuses["drift"]["company_id"] == company_id


def test_api_blocks_the_remaining_decisions_without_a_company(client: TestClient):
    review_id = str(uuid.uuid4())
    split_id = str(uuid.uuid4())
    quarantine_id = str(uuid.uuid4())
    drift_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app.users (username, password_hash, role)
            VALUES ('libera', %s, 'approver')
            """,
            (password_hash("libera-secret"),),
        )
        conn.execute(
            """
            INSERT INTO app.identity_reviews (
              id, entity_type, left_key, right_key, score, reason, status
            )
            VALUES (%s, 'Customer', 'A', 'B', 0.72, 'Nomi vicini senza identità comune', 'open'),
                   (%s, 'Customer', 'C', 'D', 0.72, 'Nomi vicini senza identità comune', 'open')
            """,
            (review_id, split_id),
        )
        conn.execute(
            """
            INSERT INTO app.quarantine_documents (id, source_name, order_key, reasons, status)
            VALUES (%s, 'mail', '1099', '[]'::jsonb, 'open')
            """,
            (quarantine_id,),
        )
        conn.execute(
            """
            INSERT INTO app.drift_reviews (id, source_name, previous_hash, new_hash, diff, status)
            VALUES (%s, 'mail', 'h1', 'h2', '{"added":["codice"]}'::jsonb, 'open')
            """,
            (drift_id,),
        )
        conn.commit()

    login(client, "mapper")
    blocked = [
        f"/api/quarantine/{quarantine_id}/release",
        f"/api/identity-reviews/{review_id}/confirm",
        f"/api/identity-reviews/{review_id}/reject",
        f"/api/identity-reviews/{split_id}/reject",
        f"/api/drift/{drift_id}/acknowledge",
    ]
    for path in blocked:
        denied = client.post(path)
        assert denied.status_code == 403, path
        assert denied.json()["detail"] == NOTICE
        assert denied.json()["detail"] == USER_WITHOUT_COMPANY

    from apps.cli import main
    from contextlib import redirect_stdout
    from io import StringIO
    import json

    created = StringIO()
    with redirect_stdout(created):
        main(["company", "create", "--name", "Decisioni restanti"])
    company_id = json.loads(created.getvalue().strip().splitlines()[-1])["company_id"]
    with redirect_stdout(StringIO()):
        main(["company", "assign", "--username", "mapper", "--company-id", company_id])

    released = client.post(f"/api/quarantine/{quarantine_id}/release")
    assert released.status_code == 200, released.text
    assert released.json()["status"] == "released"
    confirmed = client.post(f"/api/identity-reviews/{review_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    separated = client.post(f"/api/identity-reviews/{split_id}/reject")
    assert separated.status_code == 200, separated.text
    assert separated.json()["status"] == "rejected"
    seen = client.post(f"/api/drift/{drift_id}/acknowledge")
    assert seen.status_code == 200, seen.text
    assert seen.json()["status"] == "acknowledged"

    client.post("/api/logout")
    logged = client.post("/api/login", json={"username": "libera", "password": "libera-secret"})
    assert logged.status_code == 200, logged.text
    from tests.test_flow import load_demo

    load_demo()
    item = client.get("/api/inbox").json()["recommendations"][0]
    denied_decision = client.post(
        f"/api/recommendations/{item['id']}/approve",
        json={"decision": "approve", "evidence_hash": item["evidence_hash"]},
    )
    assert denied_decision.status_code == 403
    assert denied_decision.json()["detail"] == NOTICE
    denied_modify = client.post(
        f"/api/recommendations/{item['id']}/modify",
        json={"decision": "modify", "evidence_hash": item["evidence_hash"], "quantity": 4},
    )
    assert denied_modify.status_code == 403
    assert denied_modify.json()["detail"] == NOTICE
    denied_reject = client.post(
        f"/api/recommendations/{item['id']}/reject",
        json={"decision": "reject", "evidence_hash": item["evidence_hash"], "reason": "non procede"},
    )
    assert denied_reject.status_code == 403
    assert denied_reject.json()["detail"] == NOTICE
    with redirect_stdout(StringIO()):
        main(["company", "assign", "--username", "libera", "--company-id", company_id])
    approved = client.post(
        f"/api/recommendations/{item['id']}/approve",
        json={"decision": "approve", "evidence_hash": item["evidence_hash"]},
    )
    assert approved.status_code == 200, approved.text

    page = (ROOT / "apps" / "web" / "app.js").read_text(encoding="utf-8")
    assert "quarantineCard(item, canWrite, state.role)" in page
    assert "identityCard(item, canWrite, state.role)" in page
    assert "driftCard(item, canWrite, state.role)" in page
    assert "inboxCard(item, state.role, canWrite)" in page
    assert 'classList.toggle("mappings-readonly", !canWrite)' in page
    rendered = subprocess.run(
        ["node", "-e", _CARD_BUTTONS],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == "ok\n"


def test_decision_without_a_company_leaves_the_proposal_unchanged():
    from services.errors import AppError
    from services.pipeline import approve
    from services.workflow.engine import modify, reject

    load_demo()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id::text AS id, evidence_hash, status
            FROM app.recommendations
            WHERE status = 'proposed'
            LIMIT 1
            """
        ).fetchone()
        before_logs = conn.execute("SELECT count(*) AS n FROM app.decisions_log").fetchone()["n"]
        before_overrides = conn.execute("SELECT count(*) AS n FROM app.agent_overrides").fetchone()["n"]
        with pytest.raises(AppError, match="azienda assegnata") as caught:
            approve(
                conn,
                row["id"],
                "approver",
                "approver",
                row["evidence_hash"],
            )
        assert caught.value.status == 403
        assert caught.value.message == USER_WITHOUT_COMPANY
        with pytest.raises(AppError, match=USER_WITHOUT_COMPANY):
            modify(
                conn,
                row["id"],
                quantity=4,
                actor="approver",
                role="approver",
                evidence_hash=row["evidence_hash"],
            )
        with pytest.raises(AppError, match=USER_WITHOUT_COMPANY):
            reject(
                conn,
                row["id"],
                reason="non procede",
                actor="approver",
                role="approver",
                evidence_hash=row["evidence_hash"],
            )
        status = conn.execute(
            "SELECT status FROM app.recommendations WHERE id = %s",
            (row["id"],),
        ).fetchone()["status"]
        logs = conn.execute("SELECT count(*) AS n FROM app.decisions_log").fetchone()["n"]
        overrides = conn.execute("SELECT count(*) AS n FROM app.agent_overrides").fetchone()["n"]
        assert status == row["status"]
        assert logs == before_logs
        assert overrides == before_overrides
        conn.rollback()


def test_ieol_demo_still_runs_without_a_company(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    import json

    from apps.cli import main
    from services.db import database_url
    from services.migrate import demo_database_url, ensure_database

    monkeypatch.setenv("IEOL_DEMO_DATABASE", "ieol_test_demo_guard")
    demo_url = demo_database_url(database_url())
    ensure_database(demo_url)
    with connect(demo_url) as conn:
        migrate(conn)
        conn.commit()
    main(["demo"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["shortage_order"]
    assert payload["dry_run"]
    with connect(demo_url) as conn:
        status = conn.execute(
            """
            SELECT status FROM app.recommendations
            WHERE status = 'dry_run_ok'
            LIMIT 1
            """
        ).fetchone()
        logs = conn.execute("SELECT count(*) AS n FROM app.decisions_log").fetchone()["n"]
    assert status is not None
    assert logs == 0


def test_api_blocks_execute_stock_unmask_and_compensate(client: TestClient):
    load_demo()
    login(client, "approver")
    item = client.get("/api/inbox").json()["recommendations"][0]
    denied_execute = client.post(f"/api/recommendations/{item['id']}/execute")
    assert denied_execute.status_code == 403
    assert denied_execute.json()["detail"] == NOTICE
    give_company("approver", "Esecuzioni")
    approved = decide(client, item)
    assert approved.status_code == 200, approved.text
    executed = client.post(f"/api/recommendations/{item['id']}/execute")
    assert executed.status_code == 200, executed.text
    with connect() as conn:
        document_id = conn.execute(
            "SELECT id::text AS id FROM app.sandbox_documents WHERE kind = 'purchase_order'"
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO app.users (username, password_hash, role)
            VALUES ('senza', %s, 'approver')
            """,
            (password_hash("senza-secret"),),
        )
        conn.commit()
    client.post("/api/logout")
    logged = client.post("/api/login", json={"username": "senza", "password": "senza-secret"})
    assert logged.status_code == 200, logged.text
    denied_compensate = client.post(f"/api/sandbox/{document_id}/compensate")
    assert denied_compensate.status_code == 403
    assert denied_compensate.json()["detail"] == NOTICE
    give_company("senza", "Compensazioni")
    compensated = client.post(f"/api/sandbox/{document_id}/compensate")
    assert compensated.status_code == 200, compensated.text
    assert compensated.json()["status"] == "compensated"

    client.post("/api/logout")
    login(client, "mapper")
    denied_stock = client.post("/api/stock/ART-X", json={"on_hand": 400})
    assert denied_stock.status_code == 403
    assert denied_stock.json()["detail"] == NOTICE
    give_company("mapper", "Giacenza api")
    denied_orphan = client.post("/api/stock/ART-X", json={"on_hand": 400})
    assert denied_orphan.status_code == 403
    assert denied_orphan.json()["detail"] == ORPHAN_STOCK

    client.post("/api/logout")
    login(client, "admin")
    order = next(row for row in client.get("/api/orders").json()["orders"] if row["business_key"] == "1024")
    denied_unmask = client.post(
        "/api/pii/unmask",
        json={"entity_id": order["customer_id"], "reason": "verifica del cliente"},
    )
    assert denied_unmask.status_code == 403
    assert denied_unmask.json()["detail"] == NOTICE
    give_company("admin", "Dati personali")
    opened = client.post(
        "/api/pii/unmask",
        json={"entity_id": order["customer_id"], "reason": "verifica del cliente"},
    )
    assert opened.status_code == 200, opened.text


def test_orphan_stock_is_corrected_only_by_ieol_admin(client: TestClient):
    login(client, "mapper")
    load_demo()
    give_company("mapper", "Officina Nord")
    denied = client.post("/api/stock/ART-X", json={"on_hand": 11})
    assert denied.status_code == 403
    assert denied.json()["detail"] == ORPHAN_STOCK
    with connect() as conn:
        unchanged = conn.execute(
            """
            SELECT f.value
            FROM app.facts f
            JOIN app.entities e ON e.id = f.entity_id
            WHERE e.type = 'StockPosition' AND e.company_id IS NULL
              AND f.property = 'on_hand' AND f.valid_to IS NULL
              AND e.business_key = 'ART-X|WH1'
            """
        ).fetchone()
    assert unchanged is not None
    assert unchanged["value"] != 11
    give_company("admin", "IEOL")
    login(client, "admin")
    allowed = client.post("/api/stock/ART-X", json={"on_hand": 11})
    assert allowed.status_code == 200, allowed.text
    with connect() as conn:
        changed = conn.execute(
            """
            SELECT f.value
            FROM app.facts f
            JOIN app.entities e ON e.id = f.entity_id
            WHERE e.type = 'StockPosition' AND f.property = 'on_hand' AND f.valid_to IS NULL
              AND e.business_key = 'ART-X|WH1'
            """
        ).fetchone()
    assert changed["value"] == 11


_CARD_BUTTONS = r"""
const { driftCard, quarantineCard, identityCard, inboxCard, customerDetail, escapeHtml } = require("./apps/web/escape.js");
const drift = { id: "d1", source_name: "ordini", diff: { added: ["x"] }, allowed_actions: [] };
const held = { id: "q1", order_key: "1099", source_name: "mail", reasons: [], allowed_actions: [] };
const review = { id: "r1", entity_type: "Customer", reason: "Nomi vicini", score: 0.72, allowed_actions: [] };
const hiddenDrift = driftCard(drift, false);
const hiddenHeld = quarantineCard(held, false);
const hiddenReview = identityCard(review, false);
if (hiddenDrift.includes("<button") || hiddenHeld.includes("<button") || hiddenReview.includes("<button")) {
  process.exit(1);
}
if (!hiddenDrift.includes("ordini") || !hiddenHeld.includes("1099") || !hiddenReview.includes("Customer")) {
  process.exit(2);
}
const shownDrift = Object.assign({}, drift, { allowed_actions: ["acknowledge"] });
if (!driftCard(shownDrift).includes("Segna visto")) process.exit(3);
const shownHeld = Object.assign({}, held, { allowed_actions: ["release"] });
if (!quarantineCard(shownHeld).includes("Rilascia")) process.exit(4);
const shownReview = Object.assign({}, review, { allowed_actions: ["confirm", "reject"] });
if (!identityCard(shownReview).includes("Conferma fusione") || !identityCard(shownReview).includes("Tieni separati")) {
  process.exit(5);
}
const item = {
  id: "i1", status: "proposed", risk: "low", order_key: "1024", problem: "manca",
  evidence_hash: "abc", actions: [], evidence: [], allowed_actions: [],
};
const notice = "Non hai ancora un'azienda assegnata. Chiedi all'amministratore di collegarti.";
const hiddenInbox = inboxCard(item, "approver", false);
if (hiddenInbox.includes("data-act") || hiddenInbox.includes("decision-bar")) process.exit(6);
if (!hiddenInbox.includes("1024") || !hiddenInbox.includes(escapeHtml(notice))) process.exit(7);
if (hiddenInbox.includes("Sola lettura")) process.exit(9);
const operatorInbox = inboxCard(item, "operator", true);
if (operatorInbox.includes("data-act") || operatorInbox.includes("Sola lettura")) process.exit(10);
if (operatorInbox.includes(notice)) process.exit(11);
const ready = Object.assign({}, item, { status: "dry_run_ok", allowed_actions: ["execute"] });
if (!inboxCard(ready, "operator").includes("Esegui")) process.exit(12);
const quiet = Object.assign({}, ready, { allowed_actions: [] });
if (inboxCard(quiet, "approver", false).includes("Esegui") || inboxCard(quiet, "admin", false).includes("data-act")) {
  process.exit(13);
}
const executed = Object.assign({}, item, { status: "executed", executed: true, allowed_actions: [] });
if (inboxCard(executed, "approver").includes(">Esegui<") || inboxCard(executed, "admin").includes(">Esegui<")) {
  process.exit(16);
}
const openDecision = Object.assign({}, item, {
  status: "dry_run_ok",
  history: [{ id: "dec-1", decision: "approve", revoked: false, actor: "approver" }],
  allowed_actions: [],
});
if (inboxCard(openDecision, "operator", true).includes(">Annulla<")) process.exit(17);
const revocable = Object.assign({}, openDecision, { allowed_actions: ["revoke"] });
if (!inboxCard(revocable, "operator", true).includes(">Annulla<")) process.exit(18);
if (!inboxCard(revocable, "admin", true).includes(">Annulla<")) process.exit(19);
const customer = { id: "c1", company_name: "ACME", business_key: "C1", orders: [], edges: [], facts: [], allowed_actions: ["unmask"] };
if (!customerDetail(customer, "operator").includes("Mostra dati personali")) process.exit(14);
const hiddenCustomer = Object.assign({}, customer, { allowed_actions: [] });
if (customerDetail(hiddenCustomer, "admin", false).includes("Mostra dati personali") || customerDetail(hiddenCustomer, "admin", false).includes("unmask-pii")) {
  process.exit(15);
}
const listed = Object.assign({}, item, { allowed_actions: ["approve", "modify", "reject"] });
if (!inboxCard(listed, "operator").includes("decision-bar")) process.exit(8);
console.log("ok");
"""
