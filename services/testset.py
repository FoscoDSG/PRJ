"""Labelled cases and the measured mapping threshold.

Outcomes come from human decisions, from a hand-written fixture, or from
synthetic fixtures used only to exercise the code. Synthetic cases are stored
and are excluded from the 30-case minimum and from threshold selection.
The agent is not a source of labels. Evaluation reads the labelled cases,
not the decisions log. The running agent is the rules baseline, so the report
does not compare the two.
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
from services.mapping.engine import HEADER_SIMILARITY_RULE, THRESHOLD, assigned_mapping

AGENT_IS_RULES = "agente = regole, nessun confronto possibile"
_LABEL_ORIGINS = frozenset({"hand_written", "synthetic"})

ROOT = Path(__file__).resolve().parents[1]
HAND_LABELS = ROOT / "evals" / "hand_labels.json"
MIN_MEASURED_CASES = 30
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
        _insert_case(conn, version, "recommendation", "human_decision", item["decision_id"], item["label"])
    for item in mappings:
        _insert_case(conn, version, "mapping", "human_decision", item["decision_id"], item["label"])
    for item in hand_recommendations:
        _insert_case(conn, version, "recommendation", _fixture_origin(item), None, _recommendation_label(item))
    for item in hand_mappings:
        _insert_case(conn, version, "mapping", _fixture_origin(item), None, _mapping_label(item))
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
    mapping_cases = [
        row["label"]
        for row in rows
        if row["case_kind"] == "mapping" and row["label_origin"] != "synthetic"
    ]
    mapping_report, threshold_row = _mapping_report(mapping_cases)
    _store_eval(conn, current, "mapping", mapping_report, HEADER_SIMILARITY_RULE)
    _store_threshold(conn, current, threshold_row, mapping_report)
    return {
        "testset_version": current,
        "minimum": MIN_MEASURED_CASES,
        "threshold": threshold_row,
        "confronto": AGENT_IS_RULES,
        "groups": {"mapping": mapping_report},
    }


def _fixture_origin(item: dict[str, Any]) -> str:
    origin = item.get("label_origin") or "hand_written"
    if origin not in _LABEL_ORIGINS:
        raise AppError(422, "Un caso manoscritto non può avere un'altra origine")
    return origin


def _human_recommendation_cases(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT d.id::text AS decision_id, d.decision, d.action_label, d.context_snapshot
        FROM app.decisions_log d
        WHERE d.action_label IS NOT NULL
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


def _human_mapping_cases(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT DISTINCT d.id::text AS decision_id, fm.source_name, fm.source_field,
               fm.target_type, fm.target_property, fm.profile
        FROM app.decisions_log d
        JOIN app.decisions_log_sources s ON s.decision_id = d.id
        JOIN app.landing_rows lr ON lr.batch_id = s.batch_id AND lr.row_index = s.row_index
        JOIN app.landing_batches lb ON lb.id = lr.batch_id
        JOIN app.field_mappings fm
          ON fm.source_name = lb.source_name
         AND fm.status = 'confirmed'
         AND fm.valid_to IS NULL
         AND jsonb_exists(lr.data, fm.source_field)
        WHERE d.action_label IS NOT NULL
          AND d.context_snapshot IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM app.decisions_log_sources unknown_source
            WHERE unknown_source.decision_id = d.id
              AND unknown_source.confidence_method = 'unknown'
          )
        ORDER BY d.id::text, fm.source_name, fm.source_field
        """
    ).fetchall()
    return [
        {
            "decision_id": row["decision_id"],
            "label": _mapping_label(row),
        }
        for row in rows
    ]


def _mapping_label(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile": item.get("profile"),
        "source_field": item.get("source_field"),
        "target_type": item.get("target_type"),
        "target_property": item.get("target_property"),
        "source_name": item.get("source_name"),
    }


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
    label: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO app.labelled_cases (id, testset_version, case_kind, label_origin, decision_id, label)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (str(uuid.uuid4()), version, kind, origin, decision_id, Json(label)),
    )


def _rule_prediction(label: dict[str, Any]) -> tuple[bool, float]:
    profile = str(label.get("profile") or "unknown")
    column = str(label.get("source_field") or "")
    target_type, target_property, confidence, _rule = assigned_mapping(profile, column)
    correct = target_type == label.get("target_type") and target_property == label.get("target_property")
    return correct, float(confidence)


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


def _mapping_report(cases: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    count = len(cases)
    if count < 2:
        report = _empty_metrics("too_small_for_split", separated=False)
        threshold = _fallback_threshold(count, "too_small_for_split", [])
        return report, threshold
    if count < MIN_MEASURED_CASES:
        folds = [[index] for index in range(count)]
        method = "leave_one_out"
        measured = False
        saved_method = "fallback"
    else:
        folds = _kfolds(count, 5)
        method = "kfold5_f1_v1"
        measured = True
        saved_method = "kfold5_f1_v1"
    y_true: list[bool] = []
    y_pred: list[bool] = []
    chosen: list[float] = []
    for fold in folds:
        held = set(fold)
        train = [cases[index] for index in range(count) if index not in held]
        test = [cases[index] for index in fold]
        threshold = _best_threshold(train)
        chosen.append(threshold)
        for label in test:
            correct, confidence = _rule_prediction(label)
            y_true.append(correct)
            y_pred.append(confidence >= threshold)
    report = _classification(y_true, y_pred, method, separated=True)
    if measured:
        saved = statistics_median(chosen)
        threshold_row = {
            "threshold": saved,
            "method": saved_method,
            "measured": True,
            "rules_version": HEADER_SIMILARITY_RULE,
            "support": count,
            "metrics": {
                "minimum": MIN_MEASURED_CASES,
                "fold_thresholds": chosen,
                "why": "mediana delle soglie scelte sui fold di training",
            },
        }
    else:
        threshold_row = _fallback_threshold(count, method, chosen)
    return report, threshold_row


def _kfolds(count: int, k: int) -> list[list[int]]:
    folds: list[list[int]] = [[] for _ in range(k)]
    for index in range(count):
        folds[index % k].append(index)
    return [fold for fold in folds if fold]


def _fallback_threshold(count: int, selection: str, fold_thresholds: list[float]) -> dict[str, Any]:
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
            "why": (
                f"sotto il minimo di {MIN_MEASURED_CASES} casi di mapping: "
                "fallback 0,75, non misurato"
            ),
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
