"""HTTP API for mapping review, object pages, the inbox, and governed actions."""

from __future__ import annotations

import os
import tempfile
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from typing import Literal

import psycopg
from fastapi import Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ontology.loader import load_merged
from services.actions.executor import compensate
from apps.api.schemas import (
    ApproveBody,
    EmptyBody,
    FileTypeDecisionBody,
    LoginBody,
    ModifyBody,
    RejectBody,
    SqlIngestBody,
    StockBody,
    UnmaskBody,
)
from services.audit_read import list_readable
from services.db import app_connect, assert_demo_database, authenticate, demo_mode, ensure_ready
from services.evidence import evidence_key
from services.pii import install_log_redaction, read_cleartext, unmask_entity
from services.migrate import require_migrated
from services.errors import AppError
from services.companies import NO_COMPANY_UPLOAD
from services.mapping.corrections import company_id_for_user
from services.pipeline import (
    acknowledge_drift,
    adjust_on_hand,
    approve,
    confirm_confident,
    confirm_mapping,
    execute_recommendation,
    ingest_csv,
    ingest_document,
    ingest_excel,
    ingest_sql,
    load_fixtures,
    record_table_type_decision,
    release_document,
    ontology,
    refresh_canonical,
    reject_mapping,
    revoke_correction,
    run_agents,
)
from services.queries import (
    get_customer,
    get_order,
    list_audit,
    list_drift,
    list_inbox,
    list_mappings,
    list_orders,
    list_table_files,
)
from services.text import jsonable
from services.workflow.engine import modify, reject

WEB = Path(__file__).resolve().parents[1] / "web"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
SourceClass = Literal["system_of_record", "operational", "external", "manual", "unknown"]
_DEV_SESSION_SECRET = "ieol-dev-only-not-for-production"


def _configured_secret() -> str | None:
    for name in ("IEOL_SESSION_SECRET", "SESSION_SECRET"):
        value = os.environ.get(name)
        if value is not None and value.strip() != "":
            return value
    return None


def session_secret() -> str:
    secret = _configured_secret()
    if secret is not None:
        return secret
    if os.environ.get("IEOL_DEV") == "1":
        return _DEV_SESSION_SECRET
    raise RuntimeError(
        "IEOL_SESSION_SECRET assente: avvio rifiutato. "
        "Imposta IEOL_SESSION_SECRET o SESSION_SECRET, oppure IEOL_DEV=1 solo in sviluppo locale."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    evidence_key()
    install_log_redaction()
    model = load_merged()
    app.state.ontology = model
    with app_connect() as conn:
        try:
            require_migrated(conn)
            read_cleartext(conn, "SELECT 1 AS ok", purpose="connessione")
            conn.commit()
            ensure_ready(conn, model.packages)
        except psycopg.errors.InsufficientPrivilege as exc:
            raise RuntimeError(
                "Privilegi di ieol_app assenti. Avvio rifiutato. Esegui `ieol migrate`."
            ) from exc
    yield


app = FastAPI(title="Intelligent Enterprise Operating Layer", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(),
    same_site="lax",
    https_only=os.environ.get("IEOL_INSECURE_COOKIES") != "1",
)
app.mount("/assets", StaticFiles(directory=WEB), name="assets")


@app.exception_handler(AppError)
async def app_error(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


def require_user(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        raise AppError(401, "Accesso richiesto")
    return user


def _ok(payload):
    return jsonable(payload)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/config")
def config() -> dict:
    return {"demo_mode": demo_mode()}


@app.post("/api/login")
def login(request: Request, body: LoginBody) -> dict:
    with app_connect() as conn:
        user = authenticate(conn, body.username, body.password)
    if user is None:
        raise AppError(401, "Credenziali non valide")
    request.session["user"] = user
    return _public_user(user)


@app.post("/api/logout")
def logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(require_user)) -> dict:
    return _public_user(user)


def _public_user(user: dict) -> dict:
    """Session fields plus whether this user already has a company.

    The company id stays on the server. The page only needs the boolean,
    and it is read again here so an assignment is visible without a new login.
    """
    with app_connect() as conn:
        company_id = company_id_for_user(conn, user["username"])
    return {**user, "demo_mode": demo_mode(), "has_company": company_id is not None}


def _require_company(username: str) -> None:
    """A user without a company can read. Uploads and decision writes stop here."""
    with app_connect() as conn:
        if company_id_for_user(conn, username) is None:
            raise AppError(403, NO_COMPANY_UPLOAD)


@app.post("/api/demo/load")
def demo_load(user: dict = Depends(require_user)) -> dict:
    if not demo_mode():
        raise AppError(403, "Il demo è chiuso")
    if user["role"] != "admin":
        raise AppError(403, "Serve il ruolo admin")
    model = ontology()
    with app_connect() as conn:
        assert_demo_database(conn)
        result = load_fixtures(conn, user["username"], user["role"], model)
        conn.commit()
    return _ok(result)


@app.post("/api/ingest/csv")
async def ingest_csv_route(
    file: UploadFile = File(...),
    source_class: SourceClass = Form(...),
    source_name: str | None = Form(None),
    user: dict = Depends(require_user),
) -> dict:
    _require_company(user["username"])
    return await _ingest_upload(file, source_name, source_class, user, excel=False)


@app.post("/api/ingest/excel")
async def ingest_excel_route(
    file: UploadFile = File(...),
    source_class: SourceClass = Form(...),
    source_name: str | None = Form(None),
    user: dict = Depends(require_user),
) -> dict:
    _require_company(user["username"])
    return await _ingest_upload(file, source_name, source_class, user, excel=True)


async def _save_upload(file: UploadFile) -> Path:
    suffix = Path(file.filename or "upload").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temporary:
        path = Path(temporary.name)
        size = 0
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                path.unlink(missing_ok=True)
                raise AppError(413, "File troppo grande")
            temporary.write(chunk)
    return path


async def _ingest_upload(
    file: UploadFile,
    source_name: str | None,
    source_class: str,
    user: dict,
    excel: bool,
) -> dict:
    path: Path | None = None
    try:
        path = await _save_upload(file)
        name = (source_name or Path(file.filename or "upload").stem).strip()
        with app_connect() as conn:
            company_id = company_id_for_user(conn, user["username"])
            if excel:
                result = ingest_excel(
                    conn, path, name, user["username"], user["role"], source_class, company_id=company_id
                )
            else:
                result = ingest_csv(
                    conn, path, name, user["username"], user["role"], source_class, company_id=company_id
                )
            conn.commit()
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
    return _ok(result)


@app.post("/api/ingest/document")
async def ingest_document_route(file: UploadFile = File(...), user: dict = Depends(require_user)) -> dict:
    _require_company(user["username"])
    path: Path | None = None
    try:
        path = await _save_upload(file)
        with app_connect() as conn:
            company_id = company_id_for_user(conn, user["username"])
            result = ingest_document(
                conn, path, user["username"], user["role"], ontology(), company_id=company_id
            )
            conn.commit()
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
    return _ok(result)


@app.get("/api/quarantine")
def quarantine(user: dict = Depends(require_user)) -> dict:
    del user
    with app_connect() as conn:
        rows = conn.execute(
            """
            SELECT id::text, source_name, order_key, reasons, status
            FROM app.quarantine_documents
            ORDER BY created_at DESC
            """
        ).fetchall()
    return _ok({"documents": list(rows)})


@app.post("/api/quarantine/{quarantine_id}/release")
def release(quarantine_id: str, user: dict = Depends(require_user)) -> dict:
    _require_company(user["username"])
    with app_connect() as conn:
        company_id = company_id_for_user(conn, user["username"])
        result = release_document(
            conn,
            quarantine_id,
            user["role"],
            ontology(),
            actor=user["username"],
            company_id=company_id,
        )
        conn.commit()
    return _ok(result)


@app.get("/api/identity-reviews")
def identity_reviews(user: dict = Depends(require_user)) -> dict:
    del user
    from services.identity.resolve import list_reviews

    with app_connect() as conn:
        return _ok({"reviews": list_reviews(conn)})


@app.post("/api/identity-reviews/{review_id}/confirm")
def confirm_identity(review_id: str, user: dict = Depends(require_user)) -> dict:
    if user["role"] not in ("admin", "mapper"):
        raise AppError(403, "Serve il ruolo mapper")
    _require_company(user["username"])
    from services.identity.resolve import confirm_review

    with app_connect() as conn:
        company_id = company_id_for_user(conn, user["username"])
        result = confirm_review(conn, review_id, actor=user["username"], company_id=company_id)
        conn.commit()
    return result


@app.post("/api/identity-reviews/{review_id}/reject")
def reject_identity(review_id: str, user: dict = Depends(require_user)) -> dict:
    if user["role"] not in ("admin", "mapper"):
        raise AppError(403, "Serve il ruolo mapper")
    _require_company(user["username"])
    from services.identity.resolve import reject_review

    with app_connect() as conn:
        company_id = company_id_for_user(conn, user["username"])
        result = reject_review(conn, review_id, actor=user["username"], company_id=company_id)
        conn.commit()
    return result


@app.post("/api/ingest/sql")
def ingest_sql_route(body: SqlIngestBody, user: dict = Depends(require_user)) -> dict:
    _require_company(user["username"])
    table = body.table
    source_name = body.source_name or table.replace(".", "_")
    with app_connect() as conn:
        company_id = company_id_for_user(conn, user["username"])
        result = ingest_sql(
            conn,
            table,
            source_name,
            user["username"],
            user["role"],
            body.source_class,
            company_id=company_id,
        )
        conn.commit()
    return _ok(result)


@app.get("/api/mappings")
def mappings(user: dict = Depends(require_user)) -> dict:
    with app_connect() as conn:
        company_id = company_id_for_user(conn, user["username"])
        return _ok({
            "mappings": list_mappings(conn, user["role"], ontology(), company_id),
            "drift": list_drift(conn),
            "files": list_table_files(conn, company_id),
        })


@app.post("/api/mappings/{mapping_id}/confirm")
def confirm(
    mapping_id: str,
    target_property_corrected: str | None = None,
    user: dict = Depends(require_user),
) -> dict:
    _require_company(user["username"])
    model = ontology()
    with app_connect() as conn:
        company_id = company_id_for_user(conn, user["username"])
        result = confirm_mapping(
            conn,
            mapping_id,
            user["username"],
            user["role"],
            model,
            target_property_corrected=target_property_corrected,
            company_id=company_id,
        )
        conn.commit()
    return _ok(result)


@app.post("/api/mappings/{mapping_id}/reject")
def reject_map(
    mapping_id: str,
    target_property_corrected: str | None = None,
    user: dict = Depends(require_user),
) -> dict:
    _require_company(user["username"])
    model = ontology()
    with app_connect() as conn:
        company_id = company_id_for_user(conn, user["username"])
        result = reject_mapping(
            conn,
            mapping_id,
            user["role"],
            actor=user["username"],
            model=model,
            target_property_corrected=target_property_corrected,
            company_id=company_id,
        )
        conn.commit()
    return _ok(result)


@app.post("/api/mappings/confirm-confident")
def confirm_high(user: dict = Depends(require_user)) -> dict:
    _require_company(user["username"])
    model = ontology()
    with app_connect() as conn:
        company_id = company_id_for_user(conn, user["username"])
        count = confirm_confident(conn, user["role"], model, actor=user["username"], company_id=company_id)
        conn.commit()
    return {"confirmed": count}


@app.post("/api/mapping-decisions/{decision_id}/revoke")
def revoke_route(
    decision_id: str,
    body: EmptyBody,
    user: dict = Depends(require_user),
) -> dict:
    del body
    _require_company(user["username"])
    with app_connect() as conn:
        company_id = company_id_for_user(conn, user["username"])
        result = revoke_correction(
            conn,
            decision_id,
            actor=user["username"],
            role=user["role"],
            company_id=company_id,
        )
        conn.commit()
    return _ok(result)


@app.post("/api/files/{file_id}/type")
def file_type_route(
    file_id: str,
    body: FileTypeDecisionBody,
    user: dict = Depends(require_user),
) -> dict:
    _require_company(user["username"])
    with app_connect() as conn:
        company_id = company_id_for_user(conn, user["username"])
        result = record_table_type_decision(
            conn,
            file_id,
            actor=user["username"],
            role=user["role"],
            company_id=company_id,
            decision=body.decision,
            correct_type=body.correct_type,
        )
        conn.commit()
    return _ok(result)


@app.post("/api/drift/{drift_id}/acknowledge")
def ack_drift(drift_id: str, user: dict = Depends(require_user)) -> dict:
    if user["role"] not in ("admin", "mapper"):
        raise AppError(403, "Serve il ruolo mapper")
    _require_company(user["username"])
    with app_connect() as conn:
        company_id = company_id_for_user(conn, user["username"])
        result = acknowledge_drift(
            conn, drift_id, user["role"], actor=user["username"], company_id=company_id
        )
        conn.commit()
    return result


@app.post("/api/materialize")
def materialize_route(user: dict = Depends(require_user)) -> dict:
    _require_company(user["username"])
    if user["role"] not in ("admin", "mapper"):
        raise AppError(403, "Serve il ruolo mapper")
    with app_connect() as conn:
        counts = refresh_canonical(conn, ontology())
        conn.commit()
    return counts


@app.post("/api/agent/run")
def agent_run(user: dict = Depends(require_user)) -> dict:
    _require_company(user["username"])
    with app_connect() as conn:
        created = run_agents(conn, actor=user["username"], role=user["role"])
        conn.commit()
    return {"recommendations": created}


@app.get("/api/orders")
def orders(user: dict = Depends(require_user)) -> dict:
    with app_connect() as conn:
        return _ok({"orders": list_orders(conn, user["role"])})


@app.get("/api/orders/{order_id}")
def order_detail(order_id: str, user: dict = Depends(require_user)) -> dict:
    with app_connect() as conn:
        detail = get_order(conn, order_id, user["role"])
    if detail is None:
        raise AppError(404, "Ordine assente")
    return _ok(detail)


@app.get("/api/customers/{customer_id}")
def customer_detail(customer_id: str, user: dict = Depends(require_user)) -> dict:
    with app_connect() as conn:
        detail = get_customer(conn, customer_id, user["role"])
    if detail is None:
        raise AppError(404, "Cliente assente")
    return _ok(detail)


@app.get("/api/inbox")
def inbox(user: dict = Depends(require_user)) -> dict:
    with app_connect() as conn:
        rows = list_inbox(conn, user["role"])
    return _ok({"recommendations": rows})


def _require_approver_company(user: dict) -> None:
    """Approve, modify and reject are human decisions. The role is checked first."""
    if user["role"] not in ("admin", "approver"):
        raise AppError(403, "Serve un approvatore")
    _require_company(user["username"])


def _finish_decision(conn, result, error: AppError | None):
    """A 409 from a stale recommendation must keep the expiry already written."""
    if error is not None and error.status == 409:
        conn.commit()
        raise error
    if error is not None:
        raise error
    conn.commit()
    return result


@app.post("/api/recommendations/{recommendation_id}/approve")
def approve_route(recommendation_id: str, body: ApproveBody, user: dict = Depends(require_user)) -> dict:
    _require_approver_company(user)
    with app_connect() as conn:
        error = None
        result = None
        try:
            result = approve(
                conn,
                recommendation_id,
                user["username"],
                user["role"],
                body.evidence_hash,
            )
        except AppError as exc:
            error = exc
        result = _finish_decision(conn, result, error)
    return _ok(result)


@app.post("/api/recommendations/{recommendation_id}/modify")
def modify_route(recommendation_id: str, body: ModifyBody, user: dict = Depends(require_user)) -> dict:
    _require_approver_company(user)
    with app_connect() as conn:
        error = None
        result = None
        try:
            result = modify(
                conn,
                recommendation_id,
                quantity=body.quantity,
                actor=user["username"],
                role=user["role"],
                evidence_hash=body.evidence_hash,
            )
        except AppError as exc:
            error = exc
        result = _finish_decision(conn, result, error)
    return _ok(result)


@app.post("/api/recommendations/{recommendation_id}/reject")
def reject_route(recommendation_id: str, body: RejectBody, user: dict = Depends(require_user)) -> dict:
    _require_approver_company(user)
    with app_connect() as conn:
        error = None
        result = None
        try:
            result = reject(
                conn,
                recommendation_id,
                reason=body.reason,
                actor=user["username"],
                role=user["role"],
                evidence_hash=body.evidence_hash,
            )
        except AppError as exc:
            error = exc
        result = _finish_decision(conn, result, error)
    return _ok(result)


@app.post("/api/recommendations/{recommendation_id}/execute")
def execute_route(recommendation_id: str, user: dict = Depends(require_user)) -> dict:
    with app_connect() as conn:
        result = execute_recommendation(
            conn, recommendation_id, user["username"], user["role"], ontology()
        )
        conn.commit()
    return _ok(result)


@app.post("/api/sandbox/{document_id}/compensate")
def compensate_route(document_id: str, user: dict = Depends(require_user)) -> dict:
    if user["role"] not in ("admin", "approver"):
        raise AppError(403, "Solo un approvatore compensa")
    with app_connect() as conn:
        result = compensate(conn, document_id, actor=user["username"], role=user["role"])
        conn.commit()
    return _ok(result)


@app.post("/api/stock/{product_key}")
def adjust_stock(product_key: str, body: StockBody, user: dict = Depends(require_user)) -> dict:
    if user["role"] not in ("admin", "mapper"):
        raise AppError(403, "Serve il ruolo mapper")
    with app_connect() as conn:
        expired = adjust_on_hand(conn, ontology(), product_key, body.on_hand)
        conn.commit()
    return {"expired": expired}


@app.get("/api/audit")
def audit(user: dict = Depends(require_user)) -> dict:
    with app_connect() as conn:
        return _ok({"events": list_audit(conn, user["role"])})


@app.get("/api/audit/readable")
def audit_readable(
    user_filter: str | None = Query(default=None, alias="user"),
    order: str | None = None,
    batch: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
    user: dict = Depends(require_user),
) -> dict:
    if user["role"] not in ("admin", "approver"):
        raise AppError(403, "Serve un approvatore")
    with app_connect() as conn:
        return _ok(
            list_readable(
                conn,
                user=user_filter,
                order=order,
                batch=batch,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
                offset=offset,
            )
        )


@app.post("/api/pii/unmask")
def pii_unmask(body: UnmaskBody, user: dict = Depends(require_user)) -> JSONResponse:
    if user["role"] != "admin":
        raise AppError(403, "Serve il ruolo admin")
    with app_connect() as conn:
        result = unmask_entity(
            conn,
            str(body.entity_id),
            body.reason,
            actor=user["username"],
            role=user["role"],
        )
        conn.commit()
    return JSONResponse(_ok(result), headers={"Cache-Control": "no-store"})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")
