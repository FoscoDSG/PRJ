import os

os.environ["DATABASE_URL"] = "postgresql://ubuntu@/ieol_test"

import pytest

from services.db import apply_schema, connect


@pytest.fixture(autouse=True)
def fresh_database():
    with connect() as conn:
        apply_schema(conn)
    yield
