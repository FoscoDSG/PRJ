"""Two companies that both use shopify do not share rows, decisions, or measures."""

from __future__ import annotations

import json
import uuid
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from apps.cli import main
from ontology.loader import load_merged
from services.canonical.store import materialize
from services.db import connect, password_hash
from services.pipeline import (
    confirm_confident,
    confirm_mapping,
    ingest_csv,
    ingest_document,
    load_fixtures,
    reject_mapping,
    release_document,
    revoke_correction,
)
from services.queries import list_mappings, list_table_files
from services.testset import (
    MIN_MEASURED_CASES,
    MIN_MEASURED_SOURCES,
    _case_source,
    _human_mapping_cases,
    _mapping_report,
    _source_folds,
    build_testset,
    evaluate_testset,
)
from services.mapping.corrections import learned_metric
from tests.test_flow import give_company


def _company(name: str) -> str:
    created = StringIO()
    with redirect_stdout(created):
        main(["company", "create", "--name", name])
    return json.loads(created.getvalue().strip().splitlines()[-1])["company_id"]


def _mapper(username: str, company_id: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app.users (username, password_hash, role, company_id)
            VALUES (%s, %s, 'mapper', %s)
            """,
            (username, password_hash("secret-mapper-1"), company_id),
        )
        conn.commit()


def _csv(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _names(conn) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT e.company_id::text AS company_id,
               coalesce(f.value_cleartext, f.value) #>> '{}' AS shown
        FROM app.entities e
        JOIN app.facts f ON f.entity_id = e.id AND f.valid_to IS NULL
        WHERE e.type = 'Customer' AND e.valid_to IS NULL AND f.property = 'company_name'
        """
    ).fetchall()
    return {row["company_id"]: row["shown"] for row in rows}


def _shopify_rows(conn, company_id: str) -> list[dict]:
    return list(
        conn.execute(
            """
            SELECT coalesce(r.data_cleartext, r.data) AS data
            FROM app.landing_rows r
            JOIN app.landing_batches b ON b.id = r.batch_id
            WHERE b.source_name = 'shopify' AND b.company_id = %s::uuid
            """,
            (company_id,),
        ).fetchall()
    )


def test_two_shopify_companies_keep_uploads_profile_mappings_and_canonical_apart(tmp_path: Path):
    company_a = _company("Azienda A")
    company_b = _company("Azienda B")
    _mapper("mapper_a", company_a)
    _mapper("mapper_b", company_b)
    model = load_merged()
    alice = _csv(tmp_path, "a.csv", "ANAG_CLI,RAGIONE_SOCIALE,EMAIL\nC1,Alice,a@example.com\n")
    bruno = _csv(tmp_path, "b.csv", "ANAG_CLI,RAGIONE_SOCIALE,EMAIL\nC1,Bruno,b@example.com\n")
    with connect() as conn:
        ingest_csv(conn, alice, "shopify", "mapper_a", "mapper", "external", company_id=company_a)
        ingest_csv(conn, bruno, "shopify", "mapper_b", "mapper", "external", company_id=company_b)
        confirm_confident(conn, "mapper", model, actor="mapper_a", company_id=company_a)
        materialize(conn, model)
        assert _names(conn) == {company_a: "Alice"}
        conn.commit()
        confirm_confident(conn, "mapper", model, actor="mapper_b", company_id=company_b)
        materialize(conn, model)

        assert [row["data"]["ragione_sociale"] for row in _shopify_rows(conn, company_a)] == ["Alice"]
        assert [row["data"]["ragione_sociale"] for row in _shopify_rows(conn, company_b)] == ["Bruno"]
        assert _names(conn) == {company_a: "Alice", company_b: "Bruno"}
        customers = conn.execute(
            """
            SELECT company_id::text AS company_id, business_key
            FROM app.entities
            WHERE type = 'Customer' AND valid_to IS NULL
            ORDER BY company_id::text
            """
        ).fetchall()
        assert {(row["company_id"], row["business_key"]) for row in customers} == {
            (company_a, "C1"),
            (company_b, "C1"),
        }

        files_a = {item["file_id"] for item in list_table_files(conn, company_a)}
        files_b = {item["file_id"] for item in list_table_files(conn, company_b)}
        assert files_a and files_b and files_a.isdisjoint(files_b)
        maps_a = list_mappings(conn, "mapper", model, company_a)
        maps_b = list_mappings(conn, "mapper", model, company_b)
        assert {item["id"] for item in maps_a}.isdisjoint({item["id"] for item in maps_b})
        assert {item["profile"] for item in maps_a if item["source_name"] == "shopify"} == {"customer"}
        assert {item["profile"] for item in maps_b if item["source_name"] == "shopify"} == {"customer"}
        assert all(item["company_id"] == company_a for item in maps_a)
        assert all(item["company_id"] == company_b for item in maps_b)

        shorter = _csv(tmp_path, "a2.csv", "ANAG_CLI,RAGIONE_SOCIALE\nC1,Alice\n")
        ingest_csv(conn, shorter, "shopify", "mapper_a", "mapper", "external", company_id=company_a)
        email_a = conn.execute(
            """
            SELECT valid_to IS NULL AS open
            FROM app.field_mappings
            WHERE company_id = %s AND source_name = 'shopify' AND source_field = 'email'
            """,
            (company_a,),
        ).fetchone()
        email_b = conn.execute(
            """
            SELECT valid_to IS NULL AS open
            FROM app.field_mappings
            WHERE company_id = %s AND source_name = 'shopify' AND source_field = 'email'
            """,
            (company_b,),
        ).fetchone()
        assert email_a["open"] is False
        assert email_b["open"] is True
        snapshots = conn.execute(
            """
            SELECT DISTINCT ON (company_id) company_id::text AS company_id, columns
            FROM app.schema_snapshots
            WHERE source_name = 'shopify'
            ORDER BY company_id, captured_at DESC
            """
        ).fetchall()
        by_company = {row["company_id"]: list(row["columns"]) for row in snapshots}
        assert "email" not in by_company[company_a]
        assert "email" in by_company[company_b]
        conn.commit()


def test_confirm_and_revoke_in_one_company_leave_the_other_shopify(tmp_path: Path):
    company_a = _company("Conferme A")
    company_b = _company("Conferme B")
    _mapper("mapper_a", company_a)
    _mapper("mapper_b", company_b)
    model = load_merged()
    note = (
        "Da: buyer@example.com\n"
        "Cliente: Alfa Srl\n"
        "P.IVA: IT12345678901\n"
        "Codice cliente: C1\n"
        "Ordine: 5001\n"
        "Data: 2026-10-12\n"
        "Importo: 100\n"
        "\n"
        "Righe:\n"
        "ART-X 1 10\n"
    )
    document = tmp_path / "ordine_shopify.txt"
    document.write_text(note, encoding="utf-8")
    with connect() as conn:
        first = ingest_document(conn, document, "mapper_a", "mapper", model, company_id=company_a)
        second = ingest_document(conn, document, "mapper_b", "mapper", model, company_id=company_b)
        assert first["reasons"] == []
        assert second["reasons"] == []
        conn.execute(
            """
            UPDATE app.field_mappings
            SET confidence = 0.91
            WHERE company_id = %s AND status = 'proposed' AND valid_to IS NULL
            """,
            (company_b,),
        )
        ingest_document(conn, document, "mapper_a", "mapper", model, company_id=company_a)
        confidences = conn.execute(
            """
            SELECT company_id::text AS company_id, confidence
            FROM app.field_mappings
            WHERE source_name LIKE 'ordine_shopify%' AND valid_to IS NULL AND status = 'proposed'
            """
        ).fetchall()
        by_company: dict[str, set[float]] = {}
        for row in confidences:
            by_company.setdefault(row["company_id"], set()).add(float(row["confidence"]))
        assert by_company[company_a] == {0.55}
        assert by_company[company_b] == {0.91}

        release_document(
            conn,
            first["id"],
            "mapper",
            model,
            actor="mapper_a",
            company_id=company_a,
        )
        statuses = conn.execute(
            """
            SELECT company_id::text AS company_id, status
            FROM app.field_mappings
            WHERE source_name LIKE 'ordine_shopify%' AND valid_to IS NULL
            """
        ).fetchall()
        assert {row["status"] for row in statuses if row["company_id"] == company_a} == {"confirmed"}
        assert {row["status"] for row in statuses if row["company_id"] == company_b} == {"proposed"}

        shop_a = _csv(tmp_path, "sa.csv", "ANAG_CLI,RAGIONE_SOCIALE\nC1,Alice\n")
        shop_b = _csv(tmp_path, "sb.csv", "ANAG_CLI,RAGIONE_SOCIALE\nC1,Bruno\n")
        ingest_csv(conn, shop_a, "shopify", "mapper_a", "mapper", "external", company_id=company_a)
        ingest_csv(conn, shop_b, "shopify", "mapper_b", "mapper", "external", company_id=company_b)
        name_a = conn.execute(
            """
            SELECT id::text AS id FROM app.field_mappings
            WHERE company_id = %s AND source_name = 'shopify' AND source_field = 'ragione_sociale'
            """,
            (company_a,),
        ).fetchone()["id"]
        name_b = conn.execute(
            """
            SELECT id::text AS id, status FROM app.field_mappings
            WHERE company_id = %s AND source_name = 'shopify' AND source_field = 'ragione_sociale'
            """,
            (company_b,),
        ).fetchone()
        confirmed = confirm_mapping(
            conn, name_a, "mapper_a", "mapper", model, company_id=company_a
        )
        still = conn.execute(
            "SELECT status FROM app.field_mappings WHERE id = %s",
            (name_b["id"],),
        ).fetchone()
        assert still["status"] == name_b["status"] == "proposed"
        revoke_correction(
            conn,
            confirmed["decision_id"],
            actor="mapper_a",
            role="mapper",
            company_id=company_a,
            reason="annullo la conferma di A",
        )
        after = conn.execute(
            """
            SELECT id::text AS id, status,
                   (SELECT count(*) FROM app.mapping_decisions d WHERE d.mapping_id = m.id) AS decisions
            FROM app.field_mappings m
            WHERE id = %s
            """,
            (name_b["id"],),
        ).fetchone()
        assert after["status"] == "proposed"
        assert after["decisions"] == 0
        own = conn.execute(
            "SELECT status FROM app.field_mappings WHERE id = %s",
            (name_a,),
        ).fetchone()
        assert own["status"] == "confirmed"
        conn.commit()


def test_measures_and_folds_count_each_companys_shopify(tmp_path: Path):
    company_a = _company("Misure A")
    company_b = _company("Misure B")
    company_c = _company("Misure C")
    _mapper("mapper_a", company_a)
    _mapper("mapper_b", company_b)
    _mapper("mapper_c", company_c)
    model = load_merged()
    with connect() as conn:
        for company_id, actor, shown in (
            (company_a, "mapper_a", "Alice"),
            (company_b, "mapper_b", "Bruno"),
        ):
            path = _csv(tmp_path, f"{actor}.csv", f"ANAG_CLI,RAGIONE_SOCIALE\nC1,{shown}\n")
            ingest_csv(conn, path, "shopify", actor, "mapper", "external", company_id=company_id)
            mapping_id = conn.execute(
                """
                SELECT id::text AS id FROM app.field_mappings
                WHERE company_id = %s AND source_name = 'shopify' AND source_field = 'ragione_sociale'
                """,
                (company_id,),
            ).fetchone()["id"]
            confirm_mapping(conn, mapping_id, actor, "mapper", model, company_id=company_id)
        labels = [item["label"] for item in _human_mapping_cases(conn)]
        keys = {_case_source(label) for label in labels}
        assert keys == {f"{company_a}\x1fshopify", f"{company_b}\x1fshopify"}
        method, folds = _source_folds(labels)
        assert method == "leave_one_source_out"
        assert len(folds) == 2
        assert {tuple(fold["test_sources"]) for fold in folds} == {
            (f"{company_a}\x1fshopify",),
            (f"{company_b}\x1fshopify",),
        }

        for company_id, actor in (
            (company_a, "mapper_a"),
            (company_b, "mapper_b"),
            (company_c, "mapper_c"),
        ):
            existing = conn.execute(
                """
                SELECT id::text AS id FROM app.landing_batches
                WHERE company_id = %s AND source_name = 'shopify'
                ORDER BY loaded_at, id
                LIMIT 1
                """,
                (company_id,),
            ).fetchone()
            if existing:
                batch_id = existing["id"]
            else:
                batch_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO app.landing_batches (
                      id, source_name, watermark, row_count, company_id, proposed_type
                    )
                    VALUES (%s, 'shopify', %s, 1, %s, 'customer')
                    """,
                    (batch_id, f"wm-{actor}", company_id),
                )
            for index in range(10):
                mapping_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO app.field_mappings (
                      id, source_name, source_field, target_type, target_property,
                      confidence, status, profile, company_id, origin, batch_id
                    )
                    VALUES (%s, 'shopify', %s, 'Customer', 'company_name', 0.91, 'proposed',
                            'customer', %s, 'rule', %s)
                    """,
                    (mapping_id, f"col_{actor}_{index}", company_id, batch_id),
                )
                confirm_mapping(conn, mapping_id, actor, "mapper", model, company_id=company_id)

        wide = [item["label"] for item in _human_mapping_cases(conn)]
        assert len(wide) >= MIN_MEASURED_CASES
        assert len({_case_source(label) for label in wide}) >= MIN_MEASURED_SOURCES
        _report, threshold = _mapping_report(wide)
        assert threshold["measured"] is True
        folded = {
            source
            for fold in threshold["metrics"]["folds"]
            for source in fold["test_sources"]
        }
        assert len(folded) == 3
        assert all(source.endswith("\x1fshopify") for source in folded)
        plain = []
        for label in wide:
            copy = dict(label)
            copy.pop("company_id", None)
            plain.append(copy)
        assert len({_case_source(label) for label in plain}) == 1
        _plain_report, plain_threshold = _mapping_report(plain)
        assert plain_threshold["measured"] is False

        learned_a = _learn(conn, model, company_a, "mapper_a", tmp_path, "email", "Alice due")
        learned_b = _learn(conn, model, company_b, "mapper_b", tmp_path, "vat", "Bruno due")
        confirmed = confirm_mapping(
            conn, learned_a, "mapper_a", "mapper", model, company_id=company_a
        )
        reject_mapping(
            conn,
            learned_b,
            "mapper",
            actor="mapper_b",
            model=model,
            target_property_corrected="source_key",
            company_id=company_b,
        )
        assert learned_metric(conn)["confirmed"] == 1
        assert learned_metric(conn)["rejected"] == 1
        conn.commit()
        revoke_correction(
            conn,
            confirmed["decision_id"],
            actor="mapper_a",
            role="mapper",
            company_id=company_a,
            reason="annullo il suggerimento di A",
        )
        metric = learned_metric(conn)
        assert metric["confirmed"] == 0
        assert metric["rejected"] == 1
        assert metric["revoked"] == {"rule": 0, "learned": 1}
        assert conn.execute(
            "SELECT status FROM app.field_mappings WHERE id = %s",
            (learned_b,),
        ).fetchone()["status"] == "rejected"
        conn.commit()


def _learn(conn, model, company_id: str, actor: str, directory: Path, corrected: str, shown: str) -> str:
    mapping_id = conn.execute(
        """
        SELECT id::text AS id FROM app.field_mappings
        WHERE company_id = %s AND source_name = 'shopify'
          AND source_field = 'ragione_sociale' AND valid_to IS NULL
        ORDER BY valid_from DESC
        LIMIT 1
        """,
        (company_id,),
    ).fetchone()["id"]
    reject_mapping(
        conn,
        mapping_id,
        "mapper",
        actor=actor,
        model=model,
        target_property_corrected=corrected,
        company_id=company_id,
    )
    path = _csv(directory, f"{actor}-learn.csv", f"ANAG_CLI,RAGIONE_SOCIALE\nC1,{shown}\n")
    ingest_csv(conn, path, "shopify", actor, "mapper", "external", company_id=company_id)
    learned = conn.execute(
        """
        SELECT id::text AS id FROM app.field_mappings
        WHERE company_id = %s AND source_name = 'shopify' AND origin = 'learned'
          AND status = 'proposed' AND valid_to IS NULL
        """,
        (company_id,),
    ).fetchone()
    assert learned is not None
    return learned["id"]


def _measure_numbers(report: dict, threshold: dict) -> dict:
    folds = threshold["metrics"].get("folds") or []
    return {
        "n": report["n"],
        "errors": report["errors"],
        "accuracy": report["accuracy"],
        "precision": report["precision"],
        "recall": report["recall"],
        "threshold": threshold["threshold"],
        "method": threshold["method"],
        "measured": threshold["measured"],
        "human_above": report["human_above"],
        "human_below": report["human_below"],
        "fold_sizes": [
            (fold.get("cases", len(fold.get("test", []))), fold.get("selection_cases", len(fold.get("train", []))))
            for fold in (report.get("folds") or folds)
        ],
    }


def test_single_company_demo_measure_matches_grouping_by_source_name():
    company_id = give_company("mapper", "Demo singola")
    model = load_merged()
    with connect() as conn:
        load_fixtures(conn, actor="mapper", role="mapper", model=model, company_id=company_id)
        rows = conn.execute(
            """
            SELECT id::text AS id FROM app.field_mappings
            WHERE company_id = %s AND valid_to IS NULL
            """,
            (company_id,),
        ).fetchall()
        for row in rows:
            confirm_mapping(conn, row["id"], "mapper", "mapper", model, company_id=company_id)
        labels = [item["label"] for item in _human_mapping_cases(conn)]
        assert labels
        assert len({label["source_name"] for label in labels}) >= 3
        report, threshold = _mapping_report(labels)
        plain = []
        for label in labels:
            copy = dict(label)
            copy.pop("company_id", None)
            plain.append(copy)
        plain_report, plain_threshold = _mapping_report(plain)
        assert _measure_numbers(report, threshold) == _measure_numbers(plain_report, plain_threshold)
        built = build_testset(conn)
        evaluated = evaluate_testset(conn)
        assert built["mapping_count"] >= len(labels)
        assert evaluated["groups"]["mapping"]["n"] == report["n"]
        assert evaluated["groups"]["mapping"]["accuracy"] == report["accuracy"]
        assert evaluated["threshold"]["threshold"] == threshold["threshold"]
        assert evaluated["threshold"]["measured"] == threshold["measured"]
        assert evaluated["threshold"]["method"] == threshold["method"]
        conn.commit()
