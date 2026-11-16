"""Two-instance concurrency tests using real threads on one shared database.

These mimic two web processes (``docker compose`` web1/web2) talking to the
same PostgreSQL (here, the test SQLite file): interleaved uploads, fetches
with heartbeat leases, and retirements must never free a set that an
in-flight job has pinned.
"""
import threading
import time
from pathlib import Path

from app import db
from app.job_runner import pin_and_run
from app.retirement import retire_jobs

DEMO = Path(__file__).resolve().parent.parent / "demo"
SAMPLES_CSV = (DEMO / "samples.csv").read_bytes()
REVISIONS_CSV = (DEMO / "revisions.csv").read_bytes()


def _seed(client):
    client.post("/uploads/samples", files={"file": ("s.csv", SAMPLES_CSV, "text/csv")})
    client.post(
        "/uploads/revisions", files={"file": ("r.csv", REVISIONS_CSV, "text/csv")}
    )


def test_two_instances_interleaved_retirement_upload_fetch(client, monkeypatch):
    monkeypatch.setenv("JOB_LEASE_TTL_SECONDS", "30")
    _seed(client)

    # Instance A finishes a job on the original sets, then starts a second
    # job that stays PENDING (pins the original sets) while other work happens.
    finished_job = pin_and_run(owner="web1")
    with db.session_scope() as session:
        original_sample, original_revision = (
            session.get(db.CurrentInput, 1).sample_set_id,
            session.get(db.CurrentInput, 1).revision_set_id,
        )

    gate = threading.Event()
    pinned_holder: dict = {}
    errors: list[Exception] = []

    def instance_a_pins_then_waits():
        try:
            with db.session_scope() as session:
                job = db.create_fetch_job(session, owner="web1-pending")
                pinned_holder["job_id"] = job.id
            gate.set()  # inputs are pinned and committed
            # Simulate a long, still-alive computation (heartbeat not needed:
            # the TTL is 30s and this window is far shorter).
            time.sleep(1.0)
            with db.session_scope() as session:
                db.materialise_fetch_job(session, pinned_holder["job_id"])
                db.release_job_lease(session, pinned_holder["job_id"])
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    t_a = threading.Thread(target=instance_a_pins_then_waits)
    t_a.start()
    assert gate.wait(timeout=5)

    # Instance B replaces both inputs and runs its own unrelated fetch.
    new_samples = (
        b"sample_id,device_id,prediction_time\n"
        b"N1,D1,2026-01-15T10:00:00Z\n"
    )
    new_revisions = (
        b"reading_id,device_id,measurement_time,known_time,temperature,revoked\n"
        b"R9,D1,2026-01-15T09:00:00Z,2026-01-15T09:30:00Z,123.0,false\n"
    )
    assert (
        client.post(
            "/uploads/samples",
            files={"file": ("v2.csv", new_samples, "text/csv")},
        ).status_code
        == 303
    )
    assert (
        client.post(
            "/uploads/revisions",
            files={"file": ("v2.csv", new_revisions, "text/csv")},
        ).status_code
        == 303
    )
    other_job = pin_and_run(owner="web2")

    # Instance B retires the finished job while A's pinned job is in flight.
    with db.session_scope() as session:
        report = retire_jobs(session, [finished_job])
    assert report.deleted_job_ids == [finished_job]
    assert pinned_holder["job_id"] not in report.deleted_job_ids

    # The original (now old) sets survive: A's PENDING job still pins them.
    with db.session_scope() as session:
        assert session.get(db.SampleSet, original_sample) is not None
        assert session.get(db.RevisionSet, original_revision) is not None
        protected = session.get(db.FetchJob, pinned_holder["job_id"])
        assert protected.status == "PENDING"

    t_a.join(timeout=10)
    assert not errors

    # After A finishes, retiring its job finally frees the original sets.
    with db.session_scope() as session:
        report2 = retire_jobs(session, [pinned_holder["job_id"]])
    assert report2.deleted_job_ids == [pinned_holder["job_id"]]
    with db.session_scope() as session:
        assert session.get(db.SampleSet, original_sample) is None
        assert session.get(db.RevisionSet, original_revision) is None
        # The unrelated job on the new inputs is intact and readable.
        assert session.get(db.FetchJob, other_job).status == "READY"


def test_demo_loader_serialises_under_concurrent_startup(client):
    """Two instances loading demo data at once create exactly one pair of sets."""
    from app import demo_loader

    results: list[bool] = []
    lock = threading.Lock()

    def load():
        ok = demo_loader.load_demo_if_empty()
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=load) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert sorted(results) == [False, True]  # one loader wins, one no-ops
    with db.session_scope() as session:
        sample_sets = session.scalars(db.select(db.SampleSet.id)).all()
        revision_sets = session.scalars(db.select(db.RevisionSet.id)).all()
        assert len(sample_sets) == 1
        assert len(revision_sets) == 1
        current = session.get(db.CurrentInput, 1)
        assert current.sample_set_id == sample_sets[0]
        assert current.revision_set_id == revision_sets[0]
