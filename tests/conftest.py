import os
import secrets

from psycopg.conninfo import conninfo_to_dict

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "postgresql://ubuntu@/ieol_test")
os.environ.pop("IEOL_DEV", None)
os.environ["IEOL_SESSION_SECRET"] = secrets.token_urlsafe(32)
os.environ.pop("SESSION_SECRET", None)
os.environ.setdefault("IEOL_INSECURE_COOKIES", "1")
os.environ["IEOL_DEMO_MODE"] = "1"
os.environ["IEOL_DEMO_PASSWORD"] = "test-password-9f"
APP_PASSWORD = "ieol-app-test-password"
PII_PASSWORD = "ieol-pii-test-password"
_dbname = conninfo_to_dict(os.environ["DATABASE_URL"]).get("dbname") or "ieol_test"
os.environ["IEOL_APP_DATABASE_URL"] = (
    f"postgresql://ieol_app:{APP_PASSWORD}@127.0.0.1:5432/{_dbname}"
)
os.environ["IEOL_PII_DATABASE_URL"] = (
    f"postgresql://ieol_pii:{PII_PASSWORD}@127.0.0.1:5432/{_dbname}"
)
os.environ["IEOL_EVIDENCE_HMAC_KEY"] = "test-hmac-key-not-for-production"

for _role in ("admin", "mapper", "operator", "approver"):
    _password = secrets.token_urlsafe(24)
    if _password.casefold() == _role.casefold():
        _password = f"x{_password}"
    os.environ[f"IEOL_SEED_{_role.upper()}_PASSWORD"] = _password

import pytest
from psycopg import sql

from services.db import connect, seed_users
from services.migrate import migrate
from tests.helpers import wipe_test_database


@pytest.fixture(autouse=True)
def fresh_database():
    with connect() as conn:
        wipe_test_database(conn)
        migrate(conn)
        dbname = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO ieol_app").format(sql.Identifier(dbname))
        )
        conn.execute(
            sql.SQL("ALTER ROLE ieol_app WITH LOGIN PASSWORD {}").format(sql.Literal(APP_PASSWORD))
        )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO ieol_pii").format(sql.Identifier(dbname))
        )
        conn.execute(
            sql.SQL("ALTER ROLE ieol_pii WITH LOGIN PASSWORD {}").format(sql.Literal(PII_PASSWORD))
        )
        seed_users(conn)
        # seed_users leaves ieol_admin false. The fixture admin stands for
        # an owner grant, which is the only way to turn the flag on.
        conn.execute(
            """
            UPDATE app.users
            SET ieol_admin = true
            WHERE username = 'admin' AND role = 'admin'
            """
        )
        conn.commit()
    yield
