import os

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "postgresql://ubuntu@/ieol_test")
os.environ.setdefault("IEOL_SESSION_SECRET", "test-session-secret")
os.environ.setdefault("IEOL_INSECURE_COOKIES", "1")

import pytest

from services.db import apply_schema, connect


@pytest.fixture(autouse=True)
def fresh_database():
    with connect() as conn:
        apply_schema(conn, seed_demo=True)
    yield
