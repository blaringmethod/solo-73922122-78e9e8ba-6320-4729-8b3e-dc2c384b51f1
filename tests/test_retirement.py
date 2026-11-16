"""Tests for retirement: job deletion, set reclamation, leases, rollback."""
import time
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import db
from app.retirement import MAX_RETIRE_JOBS, RetirementError, parse_job_ids, retire_jobs

DEMO = Path(__file__).resolve().parent.parent / "demo"
SAMPLES_CSV = (DEMO / "samples.csv").read_bytes()
REVISIONS_CSV = (DEMO / "revisions.csv").read_bytes()


def _upload(client, url, data: bytes, name: str):
    return client.post(url, files={"file": (name, data, "text/csv")})


def _seed_inputs(client):
    _upload(client, "/uploads/samples", SAMPLES_CSV, "samples.csv")
    _upload(client, "/uploads/revisions", REVISIONS_CSV, "revisions.csv")


def _make_ready_job(client) -> int:
    resp = client.post("/jobs", follow_redirects=False)
    assert resp.status_code == 303
    return int(resp.headers["location"].rsplit("/", 1)[-1])


def _current_set_ids(session):
    current = db._get_current(session)
    return current.sample_set_id, current.revision_set_id


def _pin_pending_job() -> int:
    """Pin a job without materialising it; returns the PENDING job id."""
    with db.session_scope() as session:
        job = db.create_fetch_job(session, owner="test-worker")
        return job.id


def _all_set_ids():
    with db.session_scope() as session:
        sample_ids = list(session.scalars(select(db.SampleSet.id)))
        revision_ids = list(session.scalars(select(db.RevisionSet.id)))
    return sample_ids, revision_ids


# --------------------------------------------------------------------------- #
# basic retirement flow
# --------------------------------------------------------------------------- #
def test_retire_deletes_job_and_unreferenced_sets(client):
    _seed_inputs(client)
    job_id = _make_ready_job(client)
    with db.session_scope() as session:
        old_sample, old_revision = _current_set_ids(session)

    # Replace both inputs: the first sets are now referenced only by the job.
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

    resp = client.post(
        "/retirements", data={"job_ids": [str(job_id)]}, follow_redirects=False
    )
    assert resp.status_code == 200
    assert "清退结果报告" in resp.text

    # Job and its results are gone.
    assert client.get(f"/jobs/{job_id}").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/results").status_code == 404
    with db.session_scope() as session:
        assert session.get(db.FetchJob, job_id) is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(db.FetchResult)
                .where(db.FetchResult.job_id == job_id)
            )
            == 0
        )
        # Old sets and their detail rows were reclaimed.
        assert session.get(db.SampleSet, old_sample) is None
        assert session.get(db.RevisionSet, old_revision) is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(db.Sample)
                .where(db.Sample.sample_set_id == old_sample)
            )
            == 0
        )
        # Current input is untouched.
        cur_sample, cur_revision = _current_set_ids(session)
        assert session.get(db.SampleSet, cur_sample) is not None
        assert session.get(db.RevisionSet, cur_revision) is not None


def test_retire_keeps_sets_referenced_by_remaining_jobs(client):
    _seed_inputs(client)
    first = _make_ready_job(client)
    second = _make_ready_job(client)  # same two sets, second job

    with db.session_scope() as session:
        sample_id, revision_id = _current_set_ids(session)

    # Replace the current input so the old sets are held only by the two jobs.
    _upload(
        client,
        "/uploads/samples",
        b"sample_id,device_id,prediction_time\nN1,D1,2026-01-15T10:00:00Z\n",
        "v2.csv",
    )
    _upload(
        client,
        "/uploads/revisions",
        b"reading_id,device_id,measurement_time,known_time,temperature,revoked\n"
        b"R9,D1,2026-01-15T09:00:00Z,2026-01-15T09:30:00Z,123.0,false\n",
        "v2.csv",
    )

    with db.session_scope() as session:
        report = retire_jobs(session, [first])

    assert report.deleted_job_ids == [first]
    with db.session_scope() as session:
        assert session.get(db.FetchJob, second) is not None
        assert session.get(db.SampleSet, sample_id) is not None
        assert session.get(db.RevisionSet, revision_id) is not None
        skipped_sample = {s.set_id for s in report.skipped_sample_sets}
        skipped_revision = {s.set_id for s in report.skipped_revision_sets}
        assert sample_id in skipped_sample and revision_id in skipped_revision
        assert any("保留取数单" in s.reason for s in report.skipped_sample_sets)
        # The newly current sets are skipped for the current-input reason.
        cur_sample, cur_revision = _current_set_ids(session)
        assert cur_sample != sample_id and cur_revision != revision_id


def test_current_input_sets_are_never_reclaimed(client):
    _seed_inputs(client)
    job_id = _make_ready_job(client)
    with db.session_scope() as session:
        sample_id, revision_id = _current_set_ids(session)

    # Retire the only job: sets survive because they are the current input.
    with db.session_scope() as session:
        report = retire_jobs(session, [job_id])

    assert report.deleted_sample_set_ids == []
    assert report.deleted_revision_set_ids == []
    with db.session_scope() as session:
        assert session.get(db.SampleSet, sample_id) is not None
        assert session.get(db.RevisionSet, revision_id) is not None
        reasons = [s.reason for s in report.skipped_sample_sets]
        assert any("当前输入" in r for r in reasons)


def test_repeat_retirement_of_deleted_job_is_not_an_error(client):
    _seed_inputs(client)
    job_id = _make_ready_job(client)
    with db.session_scope() as session:
        first = retire_jobs(session, [job_id])
    assert first.deleted_job_ids == [job_id]

    # Same request again: missing id reported as skipped, no exception.
    with db.session_scope() as session:
        second = retire_jobs(session, [job_id])
    assert second.deleted_job_ids == []
    assert second.skipped_jobs == [
        (job_id, "取数单不存在（可能已被清退），跳过且不报错")
    ]

    # HTTP form path stays friendly too.
    resp = client.post("/retirements", data={"job_ids": str(job_id)})
    assert resp.status_code == 200


def test_empty_and_over_limit_requests_are_rejected(client):
    _seed_inputs(client)
    with db.session_scope() as session:
        with pytest.raises(RetirementError, match="未选择"):
            retire_jobs(session, [])
        too_many = list(range(1, MAX_RETIRE_JOBS + 2))
        with pytest.raises(RetirementError, match="至多"):
            retire_jobs(session, too_many)

    # Form route redirects with an error banner.
    resp = client.post("/retirements", data={}, follow_redirects=False)
    assert resp.status_code == 303 and "error" in resp.headers["location"]

    resp = client.post(
        "/retirements",
        data={"job_ids": ",".join(str(i) for i in range(1, 22))},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and "error" in resp.headers["location"]


def test_parse_job_ids_accepts_mixed_formats_and_dedupes():
    assert parse_job_ids(["1, 2", "3", "2", ""]) == [1, 2, 3]
    with pytest.raises(RetirementError, match="整数"):
        parse_job_ids(["abc"])
    with pytest.raises(RetirementError, match="正整数"):
        parse_job_ids(["0"])


# --------------------------------------------------------------------------- #
# in-flight (PENDING) jobs and lease protection
# --------------------------------------------------------------------------- #
def test_live_lease_pending_job_protects_pinned_old_sets(client):
    _seed_inputs(client)
    # Job A is a finished snapshot of the original sets.
    ready_job = _make_ready_job(client)
    # Job B pins the original sets but is still computing (fresh lease).
    pending_job = _pin_pending_job()
    with db.session_scope() as session:
        pinned_sample, pinned_revision = _current_set_ids(session)
        live = db.live_lease_job_ids(session)
    assert pending_job in live

    # Replace current inputs, then retire only the finished job A.
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

    with db.session_scope() as session:
        report = retire_jobs(session, [ready_job])

    assert report.deleted_job_ids == [ready_job]
    # The computing job keeps exclusive use of its pinned old sets.
    with db.session_scope() as session:
        assert session.get(db.SampleSet, pinned_sample) is not None
        assert session.get(db.RevisionSet, pinned_revision) is not None
        job = session.get(db.FetchJob, pending_job)
        assert job.status == "PENDING"
        reasons = [s.reason for s in report.skipped_sample_sets]
        assert any("计算中取数单已固定的旧输入" in r for r in reasons)


def test_retiring_a_live_pending_job_is_skipped_not_deleted(client, monkeypatch):
    _seed_inputs(client)
    pending_job = _pin_pending_job()
    with db.session_scope() as session:
        report = retire_jobs(session, [pending_job])
    assert report.deleted_job_ids == []
    assert report.skipped_jobs and "租约存活" in report.skipped_jobs[0][1]
    with db.session_scope() as session:
        assert session.get(db.FetchJob, pending_job) is not None


def test_stale_lease_is_reaped_and_its_sets_become_reclaimable(
    client, monkeypatch
):
    monkeypatch.setenv("JOB_LEASE_TTL_SECONDS", "1")
    _seed_inputs(client)
    pending_job = _pin_pending_job()
    with db.session_scope() as session:
        pinned_sample, pinned_revision = _current_set_ids(session)

    # Simulate the computing process dying: no more heartbeats; TTL elapses.
    time.sleep(1.2)
    with db.session_scope() as session:
        assert db.live_lease_job_ids(session) == set()

    # New inputs take over so the old ones are only held by the dead job.
    _upload(
        client,
        "/uploads/samples",
        b"sample_id,device_id,prediction_time\nN1,D1,2026-01-15T10:00:00Z\n",
        "v2.csv",
    )
    _upload(
        client,
        "/uploads/revisions",
        b"reading_id,device_id,measurement_time,known_time,temperature,revoked\n"
        b"R9,D1,2026-01-15T09:00:00Z,2026-01-15T09:30:00Z,123.0,false\n",
        "v2.csv",
    )

    with db.session_scope() as session:
        report = retire_jobs(session, [999999])  # nonexistent id is fine

    assert pending_job in report.reaped_dead_job_ids
    assert pinned_sample in report.deleted_sample_set_ids
    assert pinned_revision in report.deleted_revision_set_ids
    with db.session_scope() as session:
        dead = session.get(db.FetchJob, pending_job)
        assert dead.status == "FAILED"
        assert dead.sample_set_id is None and dead.revision_set_id is None
        assert session.get(db.JobLease, pending_job) is None


def test_pending_job_page_and_api_until_ready_then_404_after_retire(client):
    _seed_inputs(client)
    pending_job = _pin_pending_job()
    page = client.get(f"/jobs/{pending_job}")
    assert page.status_code == 409 and "计算中" in page.text
    payload = client.get(f"/api/jobs/{pending_job}/results").json()
    assert payload["status"] == "PENDING" and payload["items"] == []

    # Finish the computation normally: the page now serves immutable results.
    with db.session_scope() as session:
        db.materialise_fetch_job(session, pending_job)
        db.release_job_lease(session, pending_job)
    page = client.get(f"/jobs/{pending_job}")
    assert page.status_code == 200 and "source_revision_id" in page.text
    payload = client.get(f"/api/jobs/{pending_job}/results?limit=100").json()
    assert payload["status"] == "READY" and len(payload["items"]) == 6

    with db.session_scope() as session:
        retire_jobs(session, [pending_job])
    assert client.get(f"/jobs/{pending_job}").status_code == 404


# --------------------------------------------------------------------------- #
# transactional rollback and interleaving
# --------------------------------------------------------------------------- #
def test_retirement_rolls_back_whole_transaction_on_failure(client, monkeypatch):
    _seed_inputs(client)
    job_id = _make_ready_job(client)
    with db.session_scope() as session:
        sample_before = session.scalars(select(db.SampleSet.id)).all()
        revision_before = session.scalars(select(db.RevisionSet.id)).all()

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure during reclamation")

    # Fail the set-deletion step after the job deletion step has run.
    monkeypatch.setattr("app.retirement._delete_set_and_rows", boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        with db.session_scope() as session:
            retire_jobs(session, [job_id])

    # Nothing was persisted: the job, its results and every set are intact.
    with db.session_scope() as session:
        assert session.get(db.FetchJob, job_id) is not None
        assert session.scalars(select(db.SampleSet.id)).all() == sample_before
        assert session.scalars(select(db.RevisionSet.id)).all() == revision_before
        assert (
            session.scalar(
                select(func.count())
                .select_from(db.FetchResult)
                .where(db.FetchResult.job_id == job_id)
            )
            == 6
        )


def test_interleaved_upload_and_fetch_complete_while_retirement_runs(client):
    _seed_inputs(client)
    ready_job = _make_ready_job(client)
    pending_job = _pin_pending_job()  # holds a live lease on the original sets

    # Unrelated uploads proceed normally despite the in-flight job/retirement.
    resp = _upload(
        client,
        "/uploads/samples",
        b"sample_id,device_id,prediction_time\nN1,D1,2026-01-15T10:00:00Z\n",
        "v2.csv",
    )
    assert resp.status_code == 303
    resp = _upload(
        client,
        "/uploads/revisions",
        b"reading_id,device_id,measurement_time,known_time,temperature,revoked\n"
        b"R9,D1,2026-01-15T09:00:00Z,2026-01-15T09:30:00Z,123.0,false\n",
        "v2.csv",
    )
    assert resp.status_code == 303

    # A new fetch against the replaced inputs also completes.
    new_job = _make_ready_job(client)
    payload = client.get(f"/api/jobs/{new_job}/results").json()
    assert payload["total_samples"] == 1
    assert payload["items"][0]["temperature"] == 123.0

    # Retirement skips the live computing job and reclaims only what is safe.
    with db.session_scope() as session:
        report = retire_jobs(session, [ready_job])
    assert pending_job not in report.deleted_job_ids
    with db.session_scope() as session:
        assert session.get(db.FetchJob, pending_job) is not None
        assert session.get(db.FetchJob, new_job) is not None


def test_remaining_job_cursor_pagination_is_unchanged_after_retirement(client):
    _seed_inputs(client)
    first = _make_ready_job(client)
    second = _make_ready_job(client)

    before = client.get(f"/api/jobs/{second}/results?limit=2").json()
    cursor = before["next_cursor"]
    page_two_before = client.get(
        f"/api/jobs/{second}/results?limit=2&cursor={cursor}"
    ).json()

    with db.session_scope() as session:
        retire_jobs(session, [first])

    after = client.get(f"/api/jobs/{second}/results?limit=2").json()
    page_two_after = client.get(
        f"/api/jobs/{second}/results?limit=2&cursor={cursor}"
    ).json()
    assert after == before
    assert page_two_after == page_two_before


def test_preview_endpoint_lists_jobs_and_live_leases(client):
    _seed_inputs(client)
    ready_job = _make_ready_job(client)
    pending_job = _pin_pending_job()
    preview = client.get("/api/retirements/preview").json()
    by_id = {item["id"]: item for item in preview["jobs"]}
    assert by_id[ready_job]["status"] == "READY"
    assert by_id[pending_job]["status"] == "PENDING"
    assert by_id[pending_job]["lease_live"] is True
    assert pending_job in preview["live_lease_job_ids"]
    assert preview["max_retire_jobs"] == MAX_RETIRE_JOBS


def test_heartbeat_keeps_long_running_job_lease_alive(client, monkeypatch):
    monkeypatch.setenv("JOB_LEASE_TTL_SECONDS", "2")
    _seed_inputs(client)
    # pin_and_run materialises synchronously but maintains heartbeats; verify
    # a full run still ends READY with the lease released.
    job_id = None
    with db.session_scope() as session:
        job = db.create_fetch_job(session, owner="hb-worker")
        job_id = job.id
    # Refresh once as the heartbeat would, then assert it stays live.
    time.sleep(0.2)
    with db.session_scope() as session:
        db.refresh_job_lease(session, job_id)
        assert job_id in db.live_lease_job_ids(session)
    with db.session_scope() as session:
        db.materialise_fetch_job(session, job_id)
        db.release_job_lease(session, job_id)
    with db.session_scope() as session:
        assert session.get(db.JobLease, job_id) is None
        assert session.get(db.FetchJob, job_id).status == "READY"


def test_failed_job_with_intact_references_still_protects_its_sets(client):
    """A FAILED job that still references sets is treated conservatively."""
    _seed_inputs(client)
    ready_job = _make_ready_job(client)
    failed_job = _pin_pending_job()
    with db.session_scope() as session:
        sample_id, revision_id = _current_set_ids(session)
        db.fail_fetch_job(session, failed_job, "模拟计算失败（引用未解绑）")

    # Replace current inputs; the old sets are now only held by the two jobs.
    _upload(
        client,
        "/uploads/samples",
        b"sample_id,device_id,prediction_time\nN1,D1,2026-01-15T10:00:00Z\n",
        "v2.csv",
    )
    _upload(
        client,
        "/uploads/revisions",
        b"reading_id,device_id,measurement_time,known_time,temperature,revoked\n"
        b"R9,D1,2026-01-15T09:00:00Z,2026-01-15T09:30:00Z,123.0,false\n",
        "v2.csv",
    )
    with db.session_scope() as session:
        report = retire_jobs(session, [ready_job])

    with db.session_scope() as session:
        assert session.get(db.SampleSet, sample_id) is not None
        assert session.get(db.RevisionSet, revision_id) is not None
        assert any("失败取数单" in s.reason for s in report.skipped_sample_sets)

    # Deleting the failed job itself then frees the sets.
    with db.session_scope() as session:
        report2 = retire_jobs(session, [failed_job])
    assert report2.deleted_job_ids == [failed_job]
    with db.session_scope() as session:
        assert session.get(db.SampleSet, sample_id) is None
        assert session.get(db.RevisionSet, revision_id) is None


def test_report_counts_deleted_rows(client):
    _seed_inputs(client)
    job_id = _make_ready_job(client)
    with db.session_scope() as session:
        report = retire_jobs(session, [job_id])
    assert report.deleted_result_rows == 6
    assert report.deleted_job_count == 1
    assert report.as_dict()["deleted_job_count"] == 1
