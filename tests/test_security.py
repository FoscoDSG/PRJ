import hashlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from services.db import connect, seed_users
from services.text import norm

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = '"><img src=x onerror=alert(1)>'


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, role: str) -> None:
    password = os.environ[f"IEOL_SEED_{role.upper()}_PASSWORD"]
    response = client.post("/api/login", json={"username": role, "password": password})
    assert response.status_code == 200, response.text


def test_login_form_has_no_prefilled_credentials(client: TestClient):
    page = client.get("/").text
    assert "value=" not in page
    assert "coincide" not in page
    assert "/assets/escape.js" in page


def _drop_session_secrets(env: dict) -> dict:
    env.pop("SESSION_SECRET", None)
    env.pop("IEOL_SESSION_SECRET", None)
    env.pop("IEOL_DEV", None)
    return env


def test_session_secret_required():
    env = _drop_session_secrets(os.environ.copy())
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, "-c", "import apps.api.main"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "SESSION_SECRET" in result.stderr + result.stdout
    assert "ieol-dev" not in (result.stderr + result.stdout).lower()


def test_blank_session_secret_is_rejected():
    env = _drop_session_secrets(os.environ.copy())
    env["SESSION_SECRET"] = "   "
    env["IEOL_SESSION_SECRET"] = "   "
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, "-c", "import apps.api.main"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "SESSION_SECRET" in result.stderr + result.stdout


def test_dev_mode_starts_without_session_secret():
    env = _drop_session_secrets(os.environ.copy())
    env["IEOL_DEV"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, "-c", "import apps.api.main"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "ieol-dev" not in result.stderr.lower()


def test_seed_rejects_password_equal_to_role(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("IEOL_SEED_ADMIN_PASSWORD", "admin")
    with connect() as conn:
        with pytest.raises(RuntimeError, match="must not equal the role name"):
            seed_users(conn)


def test_seeded_hashes_are_argon2id():
    with connect() as conn:
        rows = conn.execute("SELECT username, password_hash, role FROM app.users").fetchall()
    assert {row["username"] for row in rows} == {"admin", "mapper", "operator", "approver"}
    for row in rows:
        stored = row["password_hash"]
        assert stored.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
        assert stored != row["username"]
        legacy = hashlib.sha256(f"ieol:{row['role']}".encode()).hexdigest()
        assert stored != legacy


def test_role_name_password_is_retired(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("IEOL_SEED_MAPPER_PASSWORD")
    legacy = hashlib.sha256(b"ieol:mapper").hexdigest()
    with connect() as conn:
        conn.execute(
            "UPDATE app.users SET password_hash = %s WHERE username = %s",
            (legacy, "mapper"),
        )
        seed_users(conn)
        conn.commit()
    denied = client.post("/api/login", json={"username": "mapper", "password": "mapper"})
    assert denied.status_code == 401
    with connect() as conn:
        stored = conn.execute(
            "SELECT password_hash FROM app.users WHERE username = %s",
            ("mapper",),
        ).fetchone()["password_hash"]
    assert stored.startswith("$argon2id$")
    assert stored != legacy


def test_role_name_password_is_replaced_from_env(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    replacement = secrets.token_urlsafe(18)
    monkeypatch.setenv("IEOL_SEED_MAPPER_PASSWORD", replacement)
    legacy = hashlib.sha256(b"ieol:mapper").hexdigest()
    with connect() as conn:
        conn.execute(
            "UPDATE app.users SET password_hash = %s WHERE username = %s",
            (legacy, "mapper"),
        )
        seed_users(conn)
        conn.commit()
    assert client.post("/api/login", json={"username": "mapper", "password": "mapper"}).status_code == 401
    allowed = client.post("/api/login", json={"username": "mapper", "password": replacement})
    assert allowed.status_code == 200, allowed.text


def test_scrypt_login_rehashes_to_argon2(client: TestClient):
    password = secrets.token_urlsafe(18)
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    stored = f"scrypt${salt.hex()}${digest.hex()}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO app.users (username, password_hash, role) VALUES (%s, %s, %s)",
            ("scrypt-user", stored, "operator"),
        )
        conn.commit()
    allowed = client.post("/api/login", json={"username": "scrypt-user", "password": password})
    assert allowed.status_code == 200, allowed.text
    with connect() as conn:
        upgraded = conn.execute(
            "SELECT password_hash FROM app.users WHERE username = %s",
            ("scrypt-user",),
        ).fetchone()["password_hash"]
    assert upgraded.startswith("$argon2id$")
    assert not upgraded.startswith("scrypt$")


def test_legacy_sha256_login_rehashes_to_argon2(client: TestClient):
    password = secrets.token_urlsafe(18)
    legacy = hashlib.sha256(f"ieol:{password}".encode()).hexdigest()
    with connect() as conn:
        conn.execute(
            "INSERT INTO app.users (username, password_hash, role) VALUES (%s, %s, %s)",
            ("legacy-user", legacy, "operator"),
        )
        conn.commit()
    denied = client.post("/api/login", json={"username": "legacy-user", "password": secrets.token_urlsafe(18)})
    assert denied.status_code == 401
    with connect() as conn:
        stored = conn.execute(
            "SELECT password_hash FROM app.users WHERE username = %s",
            ("legacy-user",),
        ).fetchone()["password_hash"]
    assert stored == legacy
    allowed = client.post("/api/login", json={"username": "legacy-user", "password": password})
    assert allowed.status_code == 200, allowed.text
    body = allowed.json()
    assert body["username"] == "legacy-user"
    assert body["role"] == "operator"
    assert isinstance(body["demo_mode"], bool)
    assert set(body) == {"username", "role", "demo_mode"}
    with connect() as conn:
        upgraded = conn.execute(
            "SELECT password_hash FROM app.users WHERE username = %s",
            ("legacy-user",),
        ).fetchone()["password_hash"]
    assert upgraded.startswith("$argon2id$")
    assert upgraded != legacy
    again = client.post("/api/login", json={"username": "legacy-user", "password": password})
    assert again.status_code == 200
    with connect() as conn:
        second = conn.execute(
            "SELECT password_hash FROM app.users WHERE username = %s",
            ("legacy-user",),
        ).fetchone()["password_hash"]
    assert second == upgraded


def test_seed_script_does_not_print_passwords():
    env = os.environ.copy()
    env["IEOL_DEV"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    secrets_in_env = [env[f"IEOL_SEED_{role.upper()}_PASSWORD"] for role in ("admin", "mapper", "operator", "approver")]
    result = subprocess.run(
        [sys.executable, "scripts/seed_dev_users.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    for secret in secrets_in_env:
        assert secret not in combined
    assert "$argon2" not in combined
    assert "sha256" not in combined


def test_malicious_csv_value_renders_inert(client: TestClient):
    _login(client, "mapper")
    ingested = client.post(
        "/api/ingest/csv",
        files={"file": ("xss_customer.csv", (ROOT / "fixtures" / "xss_customer.csv").read_bytes(), "text/csv")},
        data={"source_name": "xss_clienti", "source_class": "external"},
    )
    assert ingested.status_code == 200, ingested.text
    mappings = client.get("/api/mappings").json()["mappings"]
    hostile_field = norm(PAYLOAD)
    assert any(item["source_field"] == hostile_field for item in mappings)
    name_mapping = next(item for item in mappings if item["source_field"] == "ragione_sociale")
    assert PAYLOAD not in name_mapping["samples"]
    assert all(sample == "••••" for sample in name_mapping["samples"])
    confirmed = client.post("/api/mappings/confirm-confident")
    assert confirmed.status_code == 200, confirmed.text
    materialized = client.post("/api/materialize")
    assert materialized.status_code == 200, materialized.text
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT entity_id::text, value_cleartext
            FROM app.facts
            WHERE property = 'company_name' AND valid_to IS NULL
            """
        ).fetchall()
    match = next(row for row in rows if row["value_cleartext"] == PAYLOAD)
    customer = client.get(f"/api/customers/{match['entity_id']}").json()
    assert customer["company_name"] == "••••"
    assert PAYLOAD not in customer["company_name"]
    rendered = subprocess.run(
        ["node", "tests/xss_render_check.js"],
        cwd=ROOT,
        input=json.dumps(
            {
                "value": PAYLOAD,
                "source_name": "xss_clienti",
                "source_field": hostile_field,
                "customer": customer,
            }
        ),
        text=True,
        capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == "inert\n"
