"""Labelled cases and the measured mapping threshold.

Outcomes come from human decisions, from a hand-written fixture, or from
synthetic fixtures used only to exercise the code. Synthetic cases are stored
and are excluded from the 30-case minimum and from threshold selection.
A human mapping decision is a label: confidence_method human, who, when.
The latest decision of any type per mapping counts, including revoked.
A revoked mapping leaves the recognition measure. A confirm and a reject
both count when they are that latest decision. A reject is an error. A
correction is a wrong prediction. The score is the one stored on the
decision. Evaluation does not recompute it.
Recognition keeps only the first batch of a company and a source. The batch
is the file, not the insert time of the mapping. A batch with no company
is not that first file, and it does not become a suggestion. A company
with is_demo true is out of column recognition and of the file-type count
entirely, including a file that would otherwise be the first. The name
is not read. Bulk confirms stay out.
Confirms and rejects of learned suggestions stay out of recognition and are
counted apart, from the origin column, as the latest decision of that
mapping. not_sure on a file type is an error of that suggestion and is not
a learning example. File-type measurement keeps the latest decision on the
first batch only.
Mapping folds are split by company and source. The same source name in
two companies is two sources, both for the three-source minimum and for
the folds. A source is never in both threshold selection and evaluation.
The agent is not a source of labels. Evaluation
reads the labelled cases, not the decisions log. The running agent is the
rules baseline, so the report does not compare the two.
"""

from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Json

from services.agent.order_agent import AGENT_VERSION
from services.errors import AppError
from services.mapping.corrections import (
    demo_company_ids,
    file_type_metric,
    learned_metric,
    legacy_null_batch_sources,
    measured_batch_sources,
    measured_first_batch_ids,
)
from services.mapping.engine import HEADER_SIMILARITY_RULE, THRESHOLD

AGENT_IS_RULES = "agente = regole, nessun confronto possibile"
_LABEL_ORIGINS = frozenset({"hand_written", "synthetic"})

ROOT = Path(__file__).resolve().parents[1]
HAND_LABELS = ROOT / "evals" / "hand_labels.json"
MIN_MEASURED_CASES = 30
MIN_MEASURED_SOURCES = 3
_SOURCE_FOLDS = 5
TESTSET_LOCK_KEY = 0x49454F4C54534554
_Z = 1.96


def load_hand_labels() -> list[dict[str, Any]]:
    if not HAND_LABELS.is_file():
        return []
    payload = json.loads(HAND_LABELS.read_text(encoding="utf-8"))
    origin = payload.get("label_origin")
    if origin not in _LABEL_ORIGINS:
        raise AppError(422, "La fixture deve dichiarare label_origin hand_written oppure synthetic")
    cases = []
    for item in payload.get("cases") or []:
        item_origin = item.get("label_origin") or origin
        if item_origin not in _LABEL_ORIGINS:
            raise AppError(422, "Un caso manoscritto non può avere un'altra origine")
        copied = dict(item)
        copied["label_origin"] = item_origin
        cases.append(copied)
    return cases


def build_testset(conn: psycopg.Connection) -> dict[str, Any]:
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (TESTSET_LOCK_KEY,))
    version = conn.execute(
        "SELECT coalesce(max(version), 0) + 1 AS version FROM app.testset_versions"
    ).fetchone()["version"]
    recommendations = _human_recommendation_cases(conn)
    mappings = _human_mapping_cases(conn)
    hand = load_hand_labels()
    hand_recommendations = [item for item in hand if item.get("case_kind") == "recommendation"]
    hand_mappings = [item for item in hand if item.get("case_kind") == "mapping"]
    recommendation_count = len(recommendations) + len(hand_recommendations)
    mapping_count = len(mappings) + len(hand_mappings)
    conn.execute(
        """
        INSERT INTO app.testset_versions (version, case_count, recommendation_count, mapping_count)
        VALUES (%s, %s, %s, %s)
        """,
        (version, recommendation_count + mapping_count, recommendation_count, mapping_count),
    )
    for item in recommendations:
        _insert_case(
            conn, version, "recommendation", "human_decision",
            item["decision_id"], None, item["label"],
        )
    for item in mappings:
        _insert_case(
            conn, version, "mapping", "human_decision",
            None, item["decision_id"], item["label"],
        )
    for item in hand_recommendations:
        _insert_case(
            conn, version, "recommendation", _fixture_origin(item),
            None, None, _recommendation_label(item),
        )
    for item in hand_mappings:
        _insert_case(
            conn, version, "mapping", _fixture_origin(item),
            None, None, _mapping_label(item),
        )
    return {
        "version": int(version),
        "case_count": recommendation_count + mapping_count,
        "recommendation_count": recommendation_count,
        "mapping_count": mapping_count,
    }


def evaluate_testset(conn: psycopg.Connection) -> dict[str, Any]:
    version = conn.execute("SELECT max(version) AS version FROM app.testset_versions").fetchone()
    if version is None or version["version"] is None:
        raise AppError(404, "Nessun test set. Esegui ieol testset build")
    current = int(version["version"])
    rows = conn.execute(
        """
        SELECT case_kind, label_origin, label
        FROM app.labelled_cases
        WHERE testset_version = %s
        ORDER BY id
        """,
        (current,),
    ).fetchall()
    candidates = [
        row["label"]
        for row in rows
        if row["case_kind"] == "mapping" and row["label_origin"] != "synthetic"
    ]
    mapping_cases, missing_source = _without_missing_source(candidates)
    mapping_report, threshold_row = _mapping_report(mapping_cases)
    above, below = _above_below(mapping_cases, float(threshold_row["threshold"]))
    bulk_rule = _bulk_rule_count(conn)
    learned = learned_metric(conn)
    file_type = file_type_metric(conn)
    mapping_report["human_above"] = above
    mapping_report["human_below"] = below
    mapping_report["bulk_rule"] = bulk_rule
    mapping_report["missing_source"] = missing_source
    mapping_report["learned"] = learned
    mapping_report["file_type"] = file_type
    threshold_row["metrics"]["human_above"] = above
    threshold_row["metrics"]["human_below"] = below
    threshold_row["metrics"]["bulk_rule"] = bulk_rule
    threshold_row["metrics"]["missing_source"] = missing_source
    threshold_row["metrics"]["learned"] = learned
    threshold_row["metrics"]["file_type"] = file_type
    _store_eval(conn, current, "mapping", mapping_report, HEADER_SIMILARITY_RULE)
    _store_threshold(conn, current, threshold_row, mapping_report)
    return {
        "testset_version": current,
        "minimum": MIN_MEASURED_CASES,
        "minimum_sources": MIN_MEASURED_SOURCES,
        "threshold": threshold_row,
        "confronto": AGENT_IS_RULES,
        "groups": {"mapping": mapping_report},
    }


def _fixture_origin(item: dict[str, Any]) -> str:
    origin = item.get("label_origin") or "hand_written"
    if origin not in _LABEL_ORIGINS:
        raise AppError(422, "Un caso manoscritto non può avere un'altra origine")
    return origin


def inbox_decision_metric(conn: psycopg.Connection) -> dict[str, int]:
    """Latest decision per recommendation. A revocation is not an approval.

    Revocations are counted on their own. An earlier approve that was later
    revoked does not stay in ``approve``.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT ON (recommendation_id) decision
        FROM app.decisions_log
        ORDER BY recommendation_id, decided_at DESC, id DESC
        """
    ).fetchall()
    counts = {"approve": 0, "reject": 0, "modify": 0, "revoked": 0}
    for row in rows:
        kind = row["decision"]
        if kind in counts:
            counts[kind] += 1
    return counts


def _human_recommendation_cases(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Latest decision of each recommendation. A revoked approval is not a case."""
    rows = conn.execute(
        """
        SELECT d.id::text AS decision_id, d.decision, d.action_label, d.context_snapshot
        FROM (
          SELECT DISTINCT ON (recommendation_id)
                 id, recommendation_id, decision, action_label, context_snapshot, decided_at
          FROM app.decisions_log
          ORDER BY recommendation_id, decided_at DESC, id DESC
        ) d
        WHERE d.decision IN ('approve', 'reject', 'modify')
          AND d.action_label IS NOT NULL
          AND d.context_snapshot IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM app.decisions_log_sources s WHERE s.decision_id = d.id
          )
          AND NOT EXISTS (
            SELECT 1 FROM app.decisions_log_sources s
            WHERE s.decision_id = d.id AND s.confidence_method = 'unknown'
          )
        ORDER BY d.decided_at, d.id
        """
    ).fetchall()
    cases = []
    for row in rows:
        cases.append(
            {
                "decision_id": row["decision_id"],
                "label": {
                    "decision": row["decision"],
                    "actions": row["action_label"],
                    "context": row["context_snapshot"],
                },
            }
        )
    return cases


def _latest_mapping_decisions(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Latest decision of any type. A revoked mapping leaves the measure."""
    return list(conn.execute(
        """
        SELECT latest.decision_id, latest.mapping_id, latest.decision,
               latest.confidence_method, latest.actor, latest.decided_at,
               latest.source_name, latest.source_field, latest.target_property,
               latest.target_property_corrected, latest.score, latest.confidence_rule,
               latest.origin, latest.batch_id, latest.mapping_company_id,
               latest.mapping_source
        FROM (
          SELECT DISTINCT ON (d.mapping_id)
                 d.id::text AS decision_id, d.mapping_id::text AS mapping_id, d.decision,
                 d.confidence_method, d.actor, d.decided_at, d.source_name, d.source_field,
                 d.target_property, d.target_property_corrected, d.score, d.confidence_rule,
                 d.origin, d.batch_id::text AS batch_id,
                 m.company_id::text AS mapping_company_id, m.source_name AS mapping_source
          FROM app.mapping_decisions d
          JOIN app.field_mappings m ON m.id = d.mapping_id
          ORDER BY d.mapping_id, d.decided_at DESC, d.id DESC
        ) latest
        WHERE latest.decision IN ('confirm', 'reject')
        """
    ).fetchall())


def _first_batch_ids(conn: psycopg.Connection) -> set[str]:
    """Earliest counted file for each company and source. Not a decision timestamp.

    NULL-company batches and demo companies are not included.
    """
    return measured_first_batch_ids(conn)


def _sources_with_a_batch(conn: psycopg.Connection) -> set[tuple[str, str]]:
    """Counted company and source pairs. Legacy NULL batches are not included."""
    return measured_batch_sources(conn)


def _human_mapping_cases(conn: psycopg.Connection) -> list[dict[str, Any]]:
    first_batches = _first_batch_ids(conn)
    seen_sources = _sources_with_a_batch(conn)
    demo = demo_company_ids(conn)
    null_sources = legacy_null_batch_sources(conn)
    rows = []
    for row in _latest_mapping_decisions(conn):
        if row["confidence_method"] != "human" or row["score"] is None:
            continue
        if row.get("origin") == "learned":
            continue
        company = row.get("mapping_company_id")
        if company in demo:
            continue
        batch_id = row.get("batch_id")
        if company is None and batch_id is not None:
            continue
        if batch_id is None:
            key = (company, row.get("mapping_source"))
            if key in seen_sources:
                continue
            if company is None and row.get("mapping_source") in null_sources:
                continue
        elif batch_id not in first_batches:
            continue
        rows.append(row)
    ordered = sorted(rows, key=lambda row: (row["source_name"] or "", row["source_field"] or "", row["decision_id"]))
    return [
        {
            "decision_id": row["decision_id"],
            "label": _mapping_label(row),
        }
        for row in ordered
    ]


def _mapping_label(item: dict[str, Any]) -> dict[str, Any]:
    label = {
        "profile": item.get("profile"),
        "source_field": item.get("source_field"),
        "target_type": item.get("target_type"),
        "target_property": item.get("target_property"),
        "source_name": item.get("source_name"),
    }
    if item.get("confidence_method"):
        label["confidence_method"] = item["confidence_method"]
    if item.get("actor"):
        label["who"] = item["actor"]
    decided_at = item.get("decided_at")
    if decided_at is not None:
        label["when"] = decided_at.isoformat()
    if item.get("decision"):
        label["decision"] = item["decision"]
    if item.get("target_property_corrected"):
        label["target_property_corrected"] = item["target_property_corrected"]
    if item.get("score") is not None:
        label["score"] = float(item["score"])
        label["confidence_rule"] = item.get("confidence_rule")
        label["correct"] = _snapshot_correct(item)
    company = item.get("mapping_company_id")
    if company is None:
        company = item.get("company_id")
    if company:
        label["company_id"] = str(company)
    return label


def _snapshot_correct(item: dict[str, Any]) -> bool:
    if item.get("decision") == "reject":
        return False
    corrected = item.get("target_property_corrected")
    proposed = item.get("target_property")
    if corrected not in (None, "") and corrected != proposed:
        return False
    if "correct" in item and item.get("decision") is None:
        return bool(item["correct"])
    return True


def _recommendation_label(item: dict[str, Any]) -> dict[str, Any]:
    actions = item.get("actions")
    if actions is None:
        actions = item.get("expected_actions") or []
    return {"actions": actions, "context": item.get("context") or {}}


def _insert_case(
    conn: psycopg.Connection,
    version: int,
    kind: str,
    origin: str,
    decision_id: str | None,
    mapping_decision_id: str | None,
    label: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO app.labelled_cases (
          id, testset_version, case_kind, label_origin, decision_id, mapping_decision_id, label
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (str(uuid.uuid4()), version, kind, origin, decision_id, mapping_decision_id, Json(label)),
    )


def _stored_score(label: dict[str, Any]) -> float:
    score = label.get("score")
    if score is None:
        raise AppError(422, "Caso di mapping senza punteggio fotografato")
    return float(score)


def _rule_prediction(label: dict[str, Any]) -> tuple[bool, float]:
    """Use the frozen score. Do not call the live mapping rule."""
    return bool(label.get("correct")), _stored_score(label)


def _best_threshold(cases: list[dict[str, Any]]) -> float:
    best_t = 0.50
    best_f1 = -1.0
    prepared = [_rule_prediction(label) for label in cases]
    for step in range(50, 100):
        threshold = step / 100
        y_true = [item[0] for item in prepared]
        y_pred = [item[1] >= threshold for item in prepared]
        score = _f1(y_true, y_pred)
        if score >= best_f1:
            best_f1 = score
            best_t = threshold
    return best_t


def _case_source(label: dict[str, Any]) -> str:
    """Fold key. An empty source stays empty.

    Otherwise the key is the company and the source name, so two companies
    that both use ``shopify`` are two sources. A missing company is the
    empty string and does not merge with another company.
    """
    name = label.get("source_name")
    if name is None or str(name).strip() == "":
        return ""
    company = label.get("company_id")
    company_text = "" if company is None or str(company).strip() == "" else str(company)
    return f"{company_text}\x1f{name}"


def _source_folds(cases: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Group by company and source. Fewer than five sources: leave one source out."""
    grouped: dict[str, list[int]] = {}
    for index, label in enumerate(cases):
        grouped.setdefault(_case_source(label), []).append(index)
    sources = sorted(grouped)
    if len(sources) < 2:
        return "single_source", []
    if len(sources) < _SOURCE_FOLDS:
        held_groups = [[source] for source in sources]
        method = "leave_one_source_out"
    else:
        buckets: list[list[str]] = [[] for _ in range(_SOURCE_FOLDS)]
        for index, source in enumerate(sources):
            buckets[index % _SOURCE_FOLDS].append(source)
        held_groups = [bucket for bucket in buckets if bucket]
        method = "source_kfold5"
    folds = []
    for held in held_groups:
        held_set = set(held)
        train_sources = [source for source in sources if source not in held_set]
        folds.append({
            "train": [index for source in train_sources for index in grouped[source]],
            "test": [index for source in held for index in grouped[source]],
            "train_sources": train_sources,
            "test_sources": list(held),
        })
    return method, folds


def _fold_report(fold: dict[str, Any]) -> dict[str, Any]:
    return {
        "sources": len(fold["test_sources"]),
        "cases": len(fold["test"]),
        "selection_sources": len(fold["train_sources"]),
        "selection_cases": len(fold["train"]),
        "test_sources": list(fold["test_sources"]),
        "selection_source_names": list(fold["train_sources"]),
    }


def _mapping_report(cases: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    count = len(cases)
    if count < 2:
        report = _empty_metrics("too_small_for_split", separated=False)
        threshold = _fallback_threshold(count, "too_small_for_split", [])
        return _annotate_counts(report, threshold, cases)
    method, folds = _source_folds(cases)
    if not folds:
        report = _empty_metrics(method, separated=False)
        report["n"] = count
        threshold = _fallback_threshold(
            count,
            method,
            [],
            why="una sola fonte: la soglia non si separa dalla valutazione",
        )
        return _annotate_counts(report, threshold, cases)
    y_true: list[bool] = []
    y_pred: list[bool] = []
    chosen: list[float] = []
    fold_reports: list[dict[str, Any]] = []
    for fold in folds:
        train_sources = set(fold["train_sources"])
        test_sources = set(fold["test_sources"])
        if train_sources & test_sources:
            raise RuntimeError("una fonte sta nella scelta della soglia e nella valutazione")
        train = [cases[index] for index in fold["train"]]
        test = [cases[index] for index in fold["test"]]
        if not train or not test:
            raise RuntimeError("fold senza casi di training o di valutazione")
        threshold = _best_threshold(train)
        chosen.append(threshold)
        for label in test:
            correct, confidence = _rule_prediction(label)
            y_true.append(correct)
            y_pred.append(confidence >= threshold)
        fold_reports.append(_fold_report(fold))
    report = _classification(y_true, y_pred, method, separated=True)
    report["folds"] = fold_reports
    source_count = len({_case_source(label) for label in cases})
    if count >= MIN_MEASURED_CASES and source_count >= MIN_MEASURED_SOURCES:
        threshold_row = {
            "threshold": statistics_median(chosen),
            "method": method,
            "measured": True,
            "rules_version": HEADER_SIMILARITY_RULE,
            "support": count,
            "metrics": {
                "minimum": MIN_MEASURED_CASES,
                "fold_thresholds": chosen,
                "folds": fold_reports,
                "why": "mediana delle soglie scelte sui fold di training, per azienda e fonte",
            },
        }
    else:
        why = None
        if count >= MIN_MEASURED_CASES and source_count < MIN_MEASURED_SOURCES:
            why = (
                f"meno di {MIN_MEASURED_SOURCES} fonti distinte (azienda e nome): "
                "fallback 0,75, non misurato"
            )
        threshold_row = _fallback_threshold(count, method, chosen, why=why)
        threshold_row["metrics"]["folds"] = fold_reports
    return _annotate_counts(report, threshold_row, cases)


def _annotate_counts(
    report: dict[str, Any],
    threshold_row: dict[str, Any],
    cases: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    above, below = _above_below(cases, float(threshold_row["threshold"]))
    report["human_above"] = above
    report["human_below"] = below
    report.setdefault("bulk_rule", 0)
    report.setdefault("missing_source", 0)
    threshold_row["metrics"]["human_above"] = above
    threshold_row["metrics"]["human_below"] = below
    return report, threshold_row


def _without_missing_source(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    usable = []
    missing = 0
    for label in cases:
        if _case_source(label) == "":
            missing += 1
        else:
            usable.append(label)
    return usable, missing


def _above_below(cases: list[dict[str, Any]], threshold: float) -> tuple[int, int]:
    above = 0
    below = 0
    for label in cases:
        score = _stored_score(label)
        if score >= threshold:
            above += 1
        else:
            below += 1
    return above, below


def _bulk_rule_count(conn: psycopg.Connection) -> int:
    row = conn.execute(
        """
        SELECT count(*) AS n
        FROM (
          SELECT DISTINCT ON (mapping_id) confidence_method, decision
          FROM app.mapping_decisions
          ORDER BY mapping_id, decided_at DESC, id DESC
        ) latest
        WHERE confidence_method = 'bulk_rule' AND decision = 'confirm'
        """
    ).fetchone()
    return int(row["n"])


def _fallback_threshold(
    count: int,
    selection: str,
    fold_thresholds: list[float],
    *,
    why: str | None = None,
) -> dict[str, Any]:
    if why is None:
        why = (
            f"sotto il minimo di {MIN_MEASURED_CASES} casi di mapping: "
            "fallback 0,75, non misurato"
        )
    return {
        "threshold": float(THRESHOLD),
        "method": "fallback",
        "measured": False,
        "rules_version": HEADER_SIMILARITY_RULE,
        "support": count,
        "metrics": {
            "minimum": MIN_MEASURED_CASES,
            "selection": selection,
            "fold_thresholds": fold_thresholds,
            "folds": [],
            "why": why,
        },
    }


def _classification(y_true: list[bool], y_pred: list[bool], method: str, *, separated: bool) -> dict[str, Any]:
    count = len(y_true)
    tp = sum(1 for truth, pred in zip(y_true, y_pred, strict=True) if truth and pred)
    fp = sum(1 for truth, pred in zip(y_true, y_pred, strict=True) if not truth and pred)
    fn = sum(1 for truth, pred in zip(y_true, y_pred, strict=True) if truth and not pred)
    errors = sum(1 for truth, pred in zip(y_true, y_pred, strict=True) if truth != pred)
    accuracy = None if count == 0 else (count - errors) / count
    precision = None if (tp + fp) == 0 else tp / (tp + fp)
    recall = None if (tp + fn) == 0 else tp / (tp + fn)
    accuracy_low, accuracy_high = _wilson(count - errors, count)
    precision_low, precision_high = _wilson(tp, tp + fp)
    recall_low, recall_high = _wilson(tp, tp + fn)
    return {
        "method": method,
        "separated": separated,
        "n": count,
        "errors": errors,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "accuracy_low": accuracy_low,
        "accuracy_high": accuracy_high,
        "precision_low": precision_low,
        "precision_high": precision_high,
        "recall_low": recall_low,
        "recall_high": recall_high,
        "folds": [],
    }


def _empty_metrics(method: str, *, separated: bool) -> dict[str, Any]:
    return {
        "method": method,
        "separated": separated,
        "n": 0,
        "errors": None,
        "accuracy": None,
        "precision": None,
        "recall": None,
        "accuracy_low": None,
        "accuracy_high": None,
        "precision_low": None,
        "precision_high": None,
        "recall_low": None,
        "recall_high": None,
        "folds": [],
    }


def _f1(y_true: list[bool], y_pred: list[bool]) -> float:
    tp = sum(1 for truth, pred in zip(y_true, y_pred, strict=True) if truth and pred)
    fp = sum(1 for truth, pred in zip(y_true, y_pred, strict=True) if not truth and pred)
    fn = sum(1 for truth, pred in zip(y_true, y_pred, strict=True) if truth and not pred)
    precision = None if (tp + fp) == 0 else tp / (tp + fp)
    recall = None if (tp + fn) == 0 else tp / (tp + fn)
    if precision is None or recall is None or precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _wilson(successes: int, count: int) -> tuple[float | None, float | None]:
    if count <= 0:
        return None, None
    proportion = successes / count
    denominator = 1 + _Z * _Z / count
    centre = (proportion + _Z * _Z / (2 * count)) / denominator
    margin = _Z * math.sqrt((proportion * (1 - proportion) + _Z * _Z / (4 * count)) / count) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def statistics_median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2)


def _store_eval(
    conn: psycopg.Connection,
    version: int,
    subject: str,
    report: dict[str, Any],
    rules_version: str,
) -> None:
    conn.execute(
        """
        INSERT INTO app.testset_evals (
          id, testset_version, subject, agent_version, rules_version, method, separated,
          n, errors, accuracy, precision, recall,
          accuracy_low, accuracy_high, precision_low, precision_high, recall_low, recall_high,
          details
        )
        VALUES (
          %s, %s, %s, %s, %s, %s, %s,
          %s, %s, %s, %s, %s,
          %s, %s, %s, %s, %s, %s,
          %s
        )
        """,
        (
            str(uuid.uuid4()),
            version,
            subject,
            AGENT_VERSION,
            rules_version,
            report["method"],
            report["separated"],
            report["n"],
            report["errors"],
            report["accuracy"],
            report["precision"],
            report["recall"],
            report["accuracy_low"],
            report["accuracy_high"],
            report["precision_low"],
            report["precision_high"],
            report["recall_low"],
            report["recall_high"],
            Json({
                "minimum": MIN_MEASURED_CASES,
                "rules_version": rules_version,
                "confronto": AGENT_IS_RULES,
                "folds": report.get("folds") or [],
            }),
        ),
    )


def _store_threshold(
    conn: psycopg.Connection,
    version: int,
    threshold_row: dict[str, Any],
    mapping_report: dict[str, Any],
) -> None:
    metrics = dict(threshold_row["metrics"])
    metrics["mapping"] = {
        "n": mapping_report["n"],
        "errors": mapping_report["errors"],
        "method": mapping_report["method"],
        "separated": mapping_report["separated"],
    }
    conn.execute(
        """
        INSERT INTO app.mapping_thresholds (
          id, threshold, method, measured, rules_version, metrics, testset_version, support
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(uuid.uuid4()),
            threshold_row["threshold"],
            threshold_row["method"],
            threshold_row["measured"],
            threshold_row["rules_version"],
            Json(metrics),
            version,
            threshold_row["support"],
        ),
    )
