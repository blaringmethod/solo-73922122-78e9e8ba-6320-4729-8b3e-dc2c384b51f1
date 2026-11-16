"""Pytest fixtures: isolated SQLite database per test, no Docker required."""
import os
import tempfile

import pytest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
# Timeout lets the lease-heartbeat thread wait out a short SQLite write lock.
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{_tmp.name}?timeout=30"
os.environ.setdefault("JOB_LEASE_TTL_SECONDS", "30")
os.environ.pop("LOAD_DEMO_ON_START", None)

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client():
    db.Base.metadata.drop_all(db.engine)
    db.Base.metadata.create_all(db.engine)
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture()
def tmp_db_path():
    return _tmp.name
