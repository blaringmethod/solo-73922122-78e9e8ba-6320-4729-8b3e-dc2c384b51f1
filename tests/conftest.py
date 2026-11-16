"""Pytest fixtures: isolated SQLite database per test, no Docker required."""
import os
import tempfile

import pytest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{_tmp.name}"
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
