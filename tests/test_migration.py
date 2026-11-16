"""Lightweight migration test, run in an isolated subprocess.

Reloading the ``app.db`` module (its engine is module-level) inside the pytest
process would break references held by sibling modules, so the legacy-database
upgrade is verified in a short-lived child process instead.
"""
import site
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _site_packages() -> str:
    # The CI/dev environment installs requirements into an isolated user base.
    for path in site.getsitepackages() + [site.getusersitepackages()]:
        if (Path(path) / "sqlalchemy").exists():
            return path
    raise RuntimeError("sqlalchemy not found in any site-packages directory")


CHILD_SCRIPT = r"""
import sys
sys.path.insert(0, %r)  # project root
sys.path.insert(0, %r)  # site-packages (isolated -I mode ignores env paths)
import os
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///" + %r
from app import db
db.init_db()
db.init_db()  # idempotency
from sqlalchemy import inspect
cols = {c["name"] for c in inspect(db.engine).get_columns("fetch_jobs")}
assert {"status", "status_detail"} <= cols, cols
assert inspect(db.engine).has_table("job_leases")
with db.session_scope() as s:
    job = s.get(db.FetchJob, 1)
    assert job.status == "READY" and job.total_samples == 3
print("migration-ok")
"""


def test_init_db_migrates_legacy_fetch_jobs_table():
    db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_file.close()
    engine = create_engine(f"sqlite+pysqlite:///{db_file.name}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE sample_sets (id INTEGER PRIMARY KEY, filename VARCHAR(260), "
                "row_count INTEGER, created_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE revision_sets (id INTEGER PRIMARY KEY, filename VARCHAR(260), "
                "row_count INTEGER, created_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE current_inputs (id INTEGER PRIMARY KEY, sample_set_id INTEGER, "
                "revision_set_id INTEGER, updated_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE fetch_jobs (id INTEGER PRIMARY KEY, sample_set_id INTEGER NOT NULL, "
                "revision_set_id INTEGER NOT NULL, total_samples INTEGER, created_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO fetch_jobs (sample_set_id, revision_set_id, total_samples, created_at) "
                "VALUES (1, 1, 3, '2026-01-01 00:00:00')"
            )
        )
    engine.dispose()

    script = CHILD_SCRIPT % (
        str(PROJECT_ROOT),
        _site_packages(),
        db_file.name,
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
    )
    assert "migration-ok" in result.stdout, result.stdout + result.stderr
    assert result.returncode == 0
