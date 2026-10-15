"""HTTP API for mapping review, object pages, the inbox, and governed actions."""

from __future__ import annotations

import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from typing import Literal

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ontology.loader import load_merged
from services.actions.executor import compensate
from apps.api.schemas import ApproveBody, LoginBody, ModifyBody, RejectBody, SqlIngestBody, StockBody
from services.db import app_connect, assert_demo_database, authenticate, connect, demo_mode, ensure_ready
from services.migrate import require_migrated
from services.errors import AppError
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
    release_document,
    ontology,
    refresh_canonical,
    reject_mapping,
    run_agents,
)
from services.queries import get_customer, get_order, list_audit, list_drift, list_inbox, list_mappings, list_orders
from services.text import jsonable
from services.workflow.engine import expire_stale, modify, reject

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
    model = load_merged()
    app.state.ontology = model
    with connect() as conn:
        require_migrated(conn)
        ensure_ready(conn, model.packages)
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
    return {**user, "demo_mode": demo_mode()}


@app.post("/api/logout")
def logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(require_user)) -> dict:
    return {**user, "demo_mode": demo_mode()}


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
    return await _ingest_upload(file, source_name, source_class, user, excel=False)


@app.post("/api/ingest/excel")
async def ingest_excel_route(
    file: UploadFile = File(...),
    source_class: SourceClass = Form(...),
    source_name: str | None = Form(None),
    user: dict = Depends(require_user),
) -> dict:
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
            if excel:
                result = ingest_excel(conn, path, name, user["username"], user["role"], source_class)
            else:
                result = ingest_csv(conn, path, name, user["username"], user["role"], source_class)
            conn.commit()
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
    return _ok(result)


@app.post("/api/ingest/document")
async def ingest_document_route(file: UploadFile = File(...), user: dict = Depends(require_user)) -> dict:
    path: Path | None = None
    try:
        path = await _save_upload(file)
        with app_connect() as conn:
            result = ingest_document(conn, path, user["username"], user["role"], ontology())
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
    with app_connect() as conn:
        result = release_document(conn, quarantine_id, user["role"], ontology())
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
    from services.identity.resolve import confirm_review

    with app_connect() as conn:
        result = confirm_review(conn, review_id)
        conn.commit()
    return result


@app.post("/api/identity-reviews/{review_id}/reject")
def reject_identity(review_id: str, user: dict = Depends(require_user)) -> dict:
    if user["role"] not in ("admin", "mapper"):
        raise AppError(403, "Serve il ruolo mapper")
    from services.identity.resolve import reject_review

    with app_connect() as conn:
        result = reject_review(conn, review_id)
        conn.commit()
    return result


@app.post("/api/ingest/sql")
def ingest_sql_route(body: SqlIngestBody, user: dict = Depends(require_user)) -> dict:
    table = body.table
    source_name = body.source_name or table.replace(".", "_")
    with app_connect() as conn:
        result = ingest_sql(conn, table, source_name, user["username"], user["role"], body.source_class)
        conn.commit()
    return _ok(result)


@app.get("/api/mappings")
def mappings(user: dict = Depends(require_user)) -> dict:
    with app_connect() as conn:
        return _ok({"mappings": list_mappings(conn, user["role"], ontology()), "drift": list_drift(conn)})


@app.post("/api/mappings/{mapping_id}/confirm")
def confirm(mapping_id: str, user: dict = Depends(require_user)) -> dict:
    model = ontology()
    with app_connect() as conn:
        result = confirm_mapping(conn, mapping_id, user["username"], user["role"], model)
        conn.commit()
    return _ok(result)


@app.post("/api/mappings/{mapping_id}/reject")
def reject_map(mapping_id: str, user: dict = Depends(require_user)) -> dict:
    with app_connect() as conn:
        result = reject_mapping(conn, mapping_id, user["role"])
        conn.commit()
    return _ok(result)


@app.post("/api/mappings/confirm-confident")
def confirm_high(user: dict = Depends(require_user)) -> dict:
    model = ontology()
    with app_connect() as conn:
        count = confirm_confident(conn, user["role"], model)
        conn.commit()
    return {"confirmed": count}


@app.post("/api/drift/{drift_id}/acknowledge")
def ack_drift(drift_id: str, user: dict = Depends(require_user)) -> dict:
    if user["role"] not in ("admin", "mapper"):
        raise AppError(403, "Serve il ruolo mapper")
    with app_connect() as conn:
        result = acknowledge_drift(conn, drift_id, user["role"])
        conn.commit()
    return result


@app.post("/api/materialize")
def materialize_route(user: dict = Depends(require_user)) -> dict:
    if user["role"] not in ("admin", "mapper"):
        raise AppError(403, "Serve il ruolo mapper")
    with app_connect() as conn:
        counts = refresh_canonical(conn, ontology())
        conn.commit()
    return counts


@app.post("/api/agent/run")
def agent_run(user: dict = Depends(require_user)) -> dict:
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
        expire_stale(conn)
        rows = list_inbox(conn, user["role"])
        conn.commit()
    return _ok({"recommendations": rows})


@app.post("/api/recommendations/{recommendation_id}/approve")
def approve_route(recommendation_id: str, body: ApproveBody, user: dict = Depends(require_user)) -> dict:
    with app_connect() as conn:
        result = approve(
            conn,
            recommendation_id,
            user["username"],
            user["role"],
            body.evidence_hash,
        )
        conn.commit()
    return _ok(result)


@app.post("/api/recommendations/{recommendation_id}/modify")
def modify_route(recommendation_id: str, body: ModifyBody, user: dict = Depends(require_user)) -> dict:
    with app_connect() as conn:
        result = modify(
            conn,
            recommendation_id,
            quantity=body.quantity,
            actor=user["username"],
            role=user["role"],
            evidence_hash=body.evidence_hash,
        )
        conn.commit()
    return _ok(result)


@app.post("/api/recommendations/{recommendation_id}/reject")
def reject_route(recommendation_id: str, body: RejectBody, user: dict = Depends(require_user)) -> dict:
    with app_connect() as conn:
        result = reject(
            conn,
            recommendation_id,
            reason=body.reason,
            actor=user["username"],
            role=user["role"],
            evidence_hash=body.evidence_hash,
        )
        conn.commit()
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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")
