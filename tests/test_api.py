"""End-to-end API/UI tests: uploads, rejection, jobs, pagination, immutability."""
from pathlib import Path

from app import db

DEMO = Path(__file__).resolve().parent.parent / "demo"
SAMPLES_CSV = (DEMO / "samples.csv").read_bytes()
REVISIONS_CSV = (DEMO / "revisions.csv").read_bytes()
BAD_SAMPLES = (DEMO / "samples_bad.csv").read_bytes()
BAD_REVISIONS = (DEMO / "revisions_bad.csv").read_bytes()

EXPECTED = {
    "S1": (20.0, "R1#2026-01-15T09:00:00+00:00"),
    "S2": (22.0, "R2#2026-01-15T10:45:00+00:00"),
    "S3": (99.0, "R5#2026-01-15T10:50:00+00:00"),
    "S4": (None, None),
    "S5": (5.0, "R6#2026-01-15T08:30:00+00:00"),
    "S6": (None, None),
}


def _upload(client, url, data: bytes, name: str):
    return client.post(url, files={"file": (name, data, "text/csv")})


def _all_pages(client, job_id, limit=2):
    """Walk the whole job with the given page size and return id->result."""
    seen = {}
    cursors = []
    cursor = None
    while True:
        url = f"/api/jobs/{job_id}/results?limit={limit}"
        if cursor is not None:
            url += f"&cursor={cursor}"
        payload = client.get(url).json()
        for item in payload["items"]:
            assert item["sample_id"] not in seen, "分页出现重复样本"
            seen[item["sample_id"]] = (
                item["temperature"],
                item["source_revision_id"],
            )
        cursors.append(cursor)
        if not payload["has_more"]:
            assert payload["next_cursor"] is None
            break
        cursor = payload["next_cursor"]
    return seen, cursors


def test_full_flow_and_expected_demo_results(client):
    assert _upload(client, "/uploads/samples", SAMPLES_CSV, "samples.csv").status_code == 303
    assert _upload(client, "/uploads/revisions", REVISIONS_CSV, "revisions.csv").status_code == 303

    resp = client.post("/jobs", follow_redirects=False)
    assert resp.status_code == 303
    job_id = int(resp.headers["location"].rsplit("/", 1)[-1])

    seen, _ = _all_pages(client, job_id, limit=2)
    assert len(seen) == 6
    assert seen == EXPECTED


def test_job_requires_both_inputs(client):
    _upload(client, "/uploads/samples", SAMPLES_CSV, "samples.csv")
    resp = client.post("/jobs", follow_redirects=False)
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]


def test_invalid_files_rejected_and_old_input_preserved(client):
    _upload(client, "/uploads/samples", SAMPLES_CSV, "samples.csv")
    _upload(client, "/uploads/revisions", REVISIONS_CSV, "revisions.csv")

    bad1 = _upload(client, "/uploads/samples", BAD_SAMPLES, "bad.csv")
    assert bad1.status_code == 303 and "error" in bad1.headers["location"]
    bad2 = _upload(client, "/uploads/revisions", BAD_REVISIONS, "bad.csv")
    assert bad2.status_code == 303 and "error" in bad2.headers["location"]

    with db.session_scope() as session:
        overview = db.get_current_overview(session)
        assert overview["sample_set"].filename == "samples.csv"
        assert overview["sample_set"].row_count == 6
        assert overview["revision_set"].filename == "revisions.csv"
        assert overview["revision_set"].row_count == 8

    # A job created after the rejected uploads still sees the old inputs.
    resp = client.post("/jobs", follow_redirects=False)
    job_id = int(resp.headers["location"].rsplit("/", 1)[-1])
    seen, _ = _all_pages(client, job_id, limit=3)
    assert seen == EXPECTED


def test_job_is_immutable_after_later_uploads(client):
    _upload(client, "/uploads/samples", SAMPLES_CSV, "samples.csv")
    _upload(client, "/uploads/revisions", REVISIONS_CSV, "revisions.csv")
    resp = client.post("/jobs", follow_redirects=False)
    job_id = int(resp.headers["location"].rsplit("/", 1)[-1])

    # Replace both inputs afterwards: the existing job must not change.
    new_samples = (
        "sample_id,device_id,prediction_time\n"
        "N1,D1,2026-01-15T10:00:00Z\n"
    ).encode()
    new_revisions = (
        "reading_id,device_id,measurement_time,known_time,temperature,revoked\n"
        "R9,D1,2026-01-15T09:00:00Z,2026-01-15T09:30:00Z,123.0,false\n"
    ).encode()
    _upload(client, "/uploads/samples", new_samples, "v2.csv")
    _upload(client, "/uploads/revisions", new_revisions, "v2.csv")

    payload = client.get(f"/api/jobs/{job_id}/results?limit=100").json()
    assert payload["total_samples"] == 6
    results = {i["sample_id"]: (i["temperature"], i["source_revision_id"])
               for i in payload["items"]}
    assert results == EXPECTED

    # A brand-new job reflects the new inputs instead.
    resp = client.post("/jobs", follow_redirects=False)
    new_job_id = int(resp.headers["location"].rsplit("/", 1)[-1])
    payload = client.get(f"/api/jobs/{new_job_id}/results?limit=100").json()
    assert payload["total_samples"] == 1
    assert payload["items"][0]["temperature"] == 123.0


def test_same_cursor_is_stable_and_pages_have_no_duplicates_or_gaps(client):
    _upload(client, "/uploads/samples", SAMPLES_CSV, "samples.csv")
    _upload(client, "/uploads/revisions", REVISIONS_CSV, "revisions.csv")
    resp = client.post("/jobs", follow_redirects=False)
    job_id = int(resp.headers["location"].rsplit("/", 1)[-1])

    seen, cursors = _all_pages(client, job_id, limit=2)
    # Retry every cursor: identical rows every time.
    for cursor in cursors:
        url = f"/api/jobs/{job_id}/results?limit=2"
        if cursor is not None:
            url += f"&cursor={cursor}"
        first = client.get(url).json()
        second = client.get(url).json()
        assert first == second
    # Full coverage, in sample_id order.
    assert list(seen) == sorted(EXPECTED)

    # Unknown job -> 404; bad limit -> 422.
    assert client.get("/api/jobs/9999/results").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/results?limit=0").status_code == 422


def test_restart_safe_pagination_with_fresh_connections(client, tmp_db_path):
    """Simulate a restart: dispose the pool and rebuild sessions between pages."""
    _upload(client, "/uploads/samples", SAMPLES_CSV, "samples.csv")
    _upload(client, "/uploads/revisions", REVISIONS_CSV, "revisions.csv")
    resp = client.post("/jobs", follow_redirects=False)
    job_id = int(resp.headers["location"].rsplit("/", 1)[-1])

    collected = []
    cursor = None
    for _ in range(10):  # far more iterations than pages; loop must terminate
        db.engine.dispose()  # drop every pooled connection, as a restart would
        with db.session_scope() as session:
            page, next_cursor, has_more = db.fetch_job_page(
                session, job_id, cursor, limit=2
            )
        collected.extend(row.sample_id for row in page)
        if not has_more:
            break
        cursor = next_cursor
    assert collected == sorted(EXPECTED)


def test_html_entry_points_render(client):
    _upload(client, "/uploads/samples", SAMPLES_CSV, "samples.csv")
    _upload(client, "/uploads/revisions", REVISIONS_CSV, "revisions.csv")
    resp = client.post("/jobs", follow_redirects=False)
    job_id = int(resp.headers["location"].rsplit("/", 1)[-1])

    home = client.get("/")
    assert home.status_code == 200 and "温度取数平台" in home.text
    page = client.get(f"/jobs/{job_id}?limit=2")
    assert page.status_code == 200 and "source_revision_id" in page.text
    assert client.get("/healthz").json() == {"status": "ok"}


def test_demo_loader_is_idempotent_and_does_not_overwrite(client):
    from app import demo_loader

    # Fresh database: both inputs missing -> demo data is loaded.
    assert demo_loader.load_demo_if_empty() is True
    with db.session_scope() as session:
        overview = db.get_current_overview(session)
        assert overview["sample_set"].row_count == 6
        assert overview["revision_set"].row_count == 8
    # Second call must be a no-op so restarts never clobber existing input.
    assert demo_loader.load_demo_if_empty() is False

    # After the user replaces one input, the loader still stays inert.
    new_samples = (
        "sample_id,device_id,prediction_time\n"
        "N1,D1,2026-01-15T10:00:00Z\n"
    ).encode()
    _upload(client, "/uploads/samples", new_samples, "v2.csv")
    assert demo_loader.load_demo_if_empty() is False
    with db.session_scope() as session:
        overview = db.get_current_overview(session)
        assert overview["sample_set"].filename == "v2.csv"
        assert overview["revision_set"].row_count == 8
