"""Fetch-job execution: pinned input -> heartbeat lease -> materialisation.

The pin (PENDING job + lease) is committed in its own short transaction before
any computation starts.  A background heartbeat keeps the lease fresh while
results are being materialised, so another instance performing retirement
knows the pinned -- possibly already superseded -- input sets are still in use
and skips them.  If this process exits, the heartbeat stops; once the lease TTL
elapses the next retirement reaps the dead job and may free its old sets.
"""
from __future__ import annotations

import threading

from . import db

# Heartbeat well inside the lease TTL (default TTL 30s).
_HEARTBEAT_INTERVAL_SECONDS = 10


class LeaseHeartbeat(threading.Thread):
    """Refresh one job's lease on a fixed cadence until stopped."""

    def __init__(self, job_id: int, owner: str):
        super().__init__(daemon=True, name=f"lease-heartbeat-{job_id}")
        self.job_id = job_id
        self.owner = owner
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(_HEARTBEAT_INTERVAL_SECONDS):
            try:
                with db.session_scope() as session:
                    db.refresh_job_lease(session, self.job_id)
            except Exception:
                # The main flow owns the job's final state; a heartbeat blip
                # (e.g. transient DB restart) must not kill the process.
                pass

    def stop(self) -> None:
        self._stop_event.set()


def run_fetch_job(job_id: int, owner: str) -> None:
    """Materialise a previously pinned job, managing its lease end to end.

    Raises on failure after marking the job FAILED; the lease is always
    released (or, if the process truly dies, simply goes stale).
    """
    heartbeat = LeaseHeartbeat(job_id, owner)
    heartbeat.start()
    try:
        with db.session_scope() as session:
            db.materialise_fetch_job(session, job_id)
    except Exception:
        with db.session_scope() as session:
            db.fail_fetch_job(session, job_id, "计算过程失败，取数单已标记为失败")
        raise
    finally:
        heartbeat.stop()
        heartbeat.join(timeout=2)
        with db.session_scope() as session:
            db.release_job_lease(session, job_id)


def pin_and_run(owner: str | None = None) -> int:
    """Pin the current input in one transaction, then materialise and return id."""
    owner = owner or db.instance_owner()
    with db.session_scope() as session:
        job = db.create_fetch_job(session, owner=owner)
        job_id = job.id
    run_fetch_job(job_id, owner)
    return job_id
