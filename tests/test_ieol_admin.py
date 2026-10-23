"""Il flag di amministratore IEOL è separato dal ruolo aziendale.

La migrazione copia chi oggi è admin. I controlli di piattaforma leggono
il flag. Il ruolo admin continua a valere per le funzioni aziendali,
come lo smascheramento.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
import psycopg

from services.db import app_connect, connect, seed_users
from services.migrate import _ensure_registry, migration_files, migration_violation
from services.pipeline import ORPHAN_STOCK
from tests.helpers import wipe_test_database
from tests.test_flow import give_company, load_demo, login

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _flags(conn) -> dict[str, bool]:
    rows = conn.execute(
        "SELECT username, role, ieol_admin FROM app.users ORDER BY username"
    ).fetchall()
    return {row["username"]: (row["role"], bool(row["ieol_admin"])) for row in rows}


def test_migration_copies_today_admin_and_the_copy_can_be_restored():
    script = (ROOT / "db" / "migrations" / "0022_ieol_admin.sql").read_text(encoding="utf-8")
    assert migration_violation(script, "0022_ieol_admin.sql") is None
    assert "DROP " not in script.upper()
    with connect() as conn:
        wipe_test_database(conn)
        _ensure_registry(conn)
        for item in migration_files():
            if item.version == "0022":
                break
            conn.execute(item.path.read_text(encoding="utf-8"))
        conn.execute(
            """
            INSERT INTO app.users (username, password_hash, role) VALUES
              ('kept-admin', 'x', 'admin'),
              ('kept-mapper', 'x', 'mapper'),
              ('kept-operator', 'x', 'operator'),
              ('kept-approver', 'x', 'approver')
            """
        )
        conn.commit()
        conn.execute(script)
        conn.commit()
        assert _flags(conn) == {
            "kept-admin": ("admin", True),
            "kept-mapper": ("mapper", False),
            "kept-operator": ("operator", False),
            "kept-approver": ("approver", False),
        }
        conn.execute("UPDATE app.users SET ieol_admin = false")
        conn.execute("UPDATE app.users SET ieol_admin = true WHERE username = 'kept-mapper'")
        conn.commit()
        conn.execute(script)
        conn.commit()
        again = _flags(conn)
        assert again["kept-admin"] == ("admin", True)
        assert again["kept-mapper"] == ("mapper", True)
        assert again["kept-operator"] == ("operator", False)
        assert again["kept-approver"] == ("approver", False)
        conn.execute("UPDATE app.users SET ieol_admin = false")
        conn.commit()
        conn.execute(script)
        conn.commit()
        assert _flags(conn) == {
            "kept-admin": ("admin", True),
            "kept-mapper": ("mapper", False),
            "kept-operator": ("operator", False),
            "kept-approver": ("approver", False),
        }


def test_app_seed_marks_only_the_builtin_admin():
    with app_connect() as conn:
        conn.execute("DELETE FROM app.users")
        seed_users(conn)
        conn.commit()
        flags = _flags(conn)
    with app_connect() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                """
                INSERT INTO app.users (username, password_hash, role, ieol_admin)
                VALUES ('altro', 'x', 'admin', true)
                """
            )
        conn.rollback()
        conn.execute(
            """
            INSERT INTO app.users (username, password_hash, role)
            VALUES ('altro-admin', 'x', 'admin')
            """
        )
        conn.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE app.users SET ieol_admin = true WHERE username = 'altro-admin'"
            )
        conn.rollback()
        extra = conn.execute(
            "SELECT ieol_admin FROM app.users WHERE username = 'altro-admin'"
        ).fetchone()
    assert flags == {
        "admin": ("admin", True),
        "mapper": ("mapper", False),
        "operator": ("operator", False),
        "approver": ("approver", False),
    }
    assert extra["ieol_admin"] is False


def test_seeded_admin_keeps_the_flag_and_the_other_roles_do_not(client: TestClient):
    for role, expected in (("admin", True), ("mapper", False), ("operator", False), ("approver", False)):
        login(client, role)
        me = client.get("/api/me").json()
        assert me["role"] == role
        assert me["ieol_admin"] is expected


def test_platform_powers_follow_the_flag_not_the_role(client: TestClient):
    login(client, "admin")
    opened = client.get("/api/payment-match/measure")
    assert opened.status_code == 200, opened.text
    assert opened.json()["scope"] == "all"
    demo = client.post("/api/demo/load")
    assert demo.status_code == 409
    assert demo.json()["detail"] == "Il demo si carica solo sul database ieol_demo"

    login(client, "mapper")
    own = client.get("/api/payment-match/measure")
    assert own.status_code == 200, own.text
    assert own.json()["scope"] == "company"
    denied = client.post("/api/demo/load")
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Serve il ruolo admin"

    with connect() as conn:
        conn.execute("UPDATE app.users SET ieol_admin = false WHERE username = 'admin'")
        conn.execute("UPDATE app.users SET ieol_admin = true WHERE username = 'mapper'")
        conn.commit()

    login(client, "admin")
    narrowed = client.get("/api/payment-match/measure")
    assert narrowed.json()["scope"] == "company"
    assert narrowed.json()["decides_rule"] is False
    still_denied = client.post("/api/demo/load")
    assert still_denied.status_code == 403

    login(client, "mapper")
    widened = client.get("/api/payment-match/measure")
    assert widened.json()["scope"] == "all"
    assert widened.json()["decides_rule"] is True
    passed = client.post("/api/demo/load")
    assert passed.status_code == 409

    unmask = client.post(
        "/api/pii/unmask",
        json={"entity_id": "00000000-0000-0000-0000-000000000000", "reason": "non è il flag"},
    )
    assert unmask.status_code == 403
    assert unmask.json()["detail"] == "Serve il ruolo admin"


def test_orphan_stock_follows_the_flag(client: TestClient):
    login(client, "mapper")
    load_demo()
    give_company("mapper", "Giacenza flag")
    give_company("admin", "Senza flag")
    with connect() as conn:
        conn.execute("UPDATE app.users SET ieol_admin = true WHERE username = 'mapper'")
        conn.execute("UPDATE app.users SET ieol_admin = false WHERE username = 'admin'")
        conn.commit()
    login(client, "admin")
    refused = client.post("/api/stock/ART-X", json={"on_hand": 11})
    assert refused.status_code == 403
    assert refused.json()["detail"] == ORPHAN_STOCK
    login(client, "mapper")
    allowed = client.post("/api/stock/ART-X", json={"on_hand": 11})
    assert allowed.status_code == 200, allowed.text


_FIXTURE_BUTTON = r"""
const { showFixtureButton } = require("./apps/web/escape.js");
if (!showFixtureButton("admin", true)) process.exit(1);
if (showFixtureButton("mapper", true)) process.exit(2);
if (showFixtureButton("admin", false)) process.exit(3);
if (showFixtureButton("admin", true, false)) process.exit(4);
if (!showFixtureButton("mapper", true, true)) process.exit(5);
if (showFixtureButton("mapper", false, true)) process.exit(6);
process.stdout.write("ok\n");
"""


def test_fixture_button_uses_the_flag_when_the_page_has_it():
    rendered = subprocess.run(
        ["node", "-e", _FIXTURE_BUTTON],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == "ok\n"
