import os
import secrets

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "postgresql://ubuntu@/ieol_test")
os.environ.pop("IEOL_DEV", None)
os.environ["IEOL_SESSION_SECRET"] = secrets.token_urlsafe(32)
os.environ.pop("SESSION_SECRET", None)
os.environ.setdefault("IEOL_INSECURE_COOKIES", "1")
os.environ["IEOL_DEMO_MODE"] = "1"
os.environ["IEOL_DEMO_PASSWORD"] = "test-password-9f"

for _role in ("admin", "mapper", "operator", "approver"):
    _password = secrets.token_urlsafe(24)
    if _password.casefold() == _role.casefold():
        _password = f"x{_password}"
    os.environ[f"IEOL_SEED_{_role.upper()}_PASSWORD"] = _password

import pytest

from services.db import connect, seed_users
from services.migrate import migrate
from tests.helpers import wipe_test_database


@pytest.fixture(autouse=True)
def fresh_database():
    with connect() as conn:
        wipe_test_database(conn)
        migrate(conn)
        seed_users(conn)
        conn.commit()
    yield
