"""Retirement (清退): delete chosen fetch jobs and reclaim unreferenced inputs.

A retirement request selects at most :data:`MAX_RETIRE_JOBS` fetch jobs.
Inside one transaction it:

1. deletes every selected job that still exists (a repeat request against an
   already deleted id is simply counted as skipped, never an error);
2. reaps PENDING jobs whose computing process is gone -- their lease heartbeat
   is older than the TTL -- by marking them FAILED and detaching their set
   references, since ``RESTRICT`` foreign keys otherwise pin the old sets;
3. deletes each sample/revision set (with all its detail rows) that is neither
   the current input nor referenced by any remaining job (including jobs still
   computing under a live lease, which keep exclusive use of their pinned
   sets); everything else is reported with an explicit skip reason.

Any failure rolls the whole transaction back, so a partial retirement can
never be observed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import delete, func, select

from . import db

MAX_RETIRE_JOBS = 20


class RetirementError(ValueError):
    """Raised when the retirement request itself is invalid."""


@dataclass
class SkippedSet:
    kind: str  # "sample" | "revision"
    set_id: int
    reason: str


@dataclass
class RetirementReport:
    requested_job_ids: list[int]
    deleted_job_ids: list[int] = field(default_factory=list)
    deleted_result_rows: int = 0
    reaped_dead_job_ids: list[int] = field(default_factory=list)
    deleted_sample_set_ids: list[int] = field(default_factory=list)
    deleted_sample_rows: int = 0
    deleted_revision_set_ids: list[int] = field(default_factory=list)
    deleted_revision_rows: int = 0
    skipped_jobs: list[tuple[int, str]] = field(default_factory=list)
    skipped_sample_sets: list[SkippedSet] = field(default_factory=list)
    skipped_revision_sets: list[SkippedSet] = field(default_factory=list)

    @property
    def deleted_job_count(self) -> int:
        return len(self.deleted_job_ids)

    def as_dict(self) -> dict:
        return {
            "requested_job_ids": self.requested_job_ids,
            "deleted_jobs": self.deleted_job_ids,
            "deleted_job_count": self.deleted_job_count,
            "deleted_result_rows": self.deleted_result_rows,
            "reaped_dead_jobs": self.reaped_dead_job_ids,
            "deleted_sample_sets": self.deleted_sample_set_ids,
            "deleted_sample_rows": self.deleted_sample_rows,
            "deleted_revision_sets": self.deleted_revision_set_ids,
            "deleted_revision_rows": self.deleted_revision_rows,
            "skipped_jobs": [
                {"job_id": job_id, "reason": reason}
                for job_id, reason in self.skipped_jobs
            ],
            "skipped_sample_sets": [vars(item) for item in self.skipped_sample_sets],
            "skipped_revision_sets": [vars(item) for item in self.skipped_revision_sets],
        }


def parse_job_ids(raw_values: list[str]) -> list[int]:
    """Parse form values (each may itself be a comma-separated list)."""
    ids: list[int] = []
    seen: set[int] = set()
    for raw in raw_values:
        for token in str(raw).split(","):
            token = token.strip()
            if not token:
                continue
            try:
                job_id = int(token)
            except ValueError as exc:
                raise RetirementError(f"取数单编号不是整数：{token!r}") from exc
            if job_id <= 0:
                raise RetirementError(f"取数单编号必须为正整数：{job_id}")
            if job_id not in seen:
                seen.add(job_id)
                ids.append(job_id)
    return ids


def _referenced_set_ids(session, live_job_ids: set[int]) -> tuple[
    dict[int, list[str]], dict[int, list[str]], set[int], set[int]
]:
    """Map each set id to human-readable reasons it is still referenced.

    Returns ``(sample_refs, revision_refs, pending_sample_ids,
    pending_revision_ids)`` where the pending sets belong to jobs still
    computing under a live lease.
    """
    sample_refs: dict[int, list[str]] = {}
    revision_refs: dict[int, list[str]] = {}
    pending_samples: set[int] = set()
    pending_revisions: set[int] = set()

    jobs = session.scalars(select(db.FetchJob)).all()
    for job in jobs:
        if job.sample_set_id is None and job.revision_set_id is None:
            continue  # already detached dead job; references nothing
        if job.status == "PENDING" and job.id in live_job_ids:
            state = "计算中取数单"
            if job.sample_set_id is not None:
                pending_samples.add(job.sample_set_id)
            if job.revision_set_id is not None:
                pending_revisions.add(job.revision_set_id)
        elif job.status == "PENDING":
            # Lease is stale: this job will be reaped further below, so it
            # must not protect its sets.
            continue
        elif job.status == "FAILED":
            state = "失败取数单"
        else:
            state = "保留取数单"
        label = f"{state} #{job.id} 引用"
        if job.sample_set_id is not None:
            sample_refs.setdefault(job.sample_set_id, []).append(label)
        if job.revision_set_id is not None:
            revision_refs.setdefault(job.revision_set_id, []).append(label)
    return sample_refs, revision_refs, pending_samples, pending_revisions


def _delete_set_and_rows(session, set_model, row_model, set_id_col, set_ids: list[int]):
    if not set_ids:
        return 0
    row_count = session.scalar(
        select(func.count())
        .select_from(row_model)
        .where(set_id_col.in_(set_ids))
    ) or 0
    session.execute(delete(row_model).where(set_id_col.in_(set_ids)))
    session.execute(delete(set_model).where(set_model.id.in_(set_ids)))
    return row_count


def retire_jobs(session, requested: list[int]) -> RetirementReport:
    """Execute a retirement; caller owns the transaction (rollback on error)."""
    if not requested:
        raise RetirementError("未选择任何取数单，请先勾选要清退的取数单（至多 20 张）")
    if len(requested) > MAX_RETIRE_JOBS:
        raise RetirementError(
            f"单次清退至多选择 {MAX_RETIRE_JOBS} 张取数单，本次选择 {len(requested)} 张"
        )

    report = RetirementReport(requested_job_ids=list(requested))
    live_job_ids = db.live_lease_job_ids(session)

    # ---- 1. delete the explicitly selected jobs ----------------------------
    selected: list[db.FetchJob] = []
    for job_id in requested:
        job = session.get(db.FetchJob, job_id)
        if job is None:
            report.skipped_jobs.append((job_id, "取数单不存在（可能已被清退），跳过且不报错"))
            continue
        if job.status == "PENDING" and job_id in live_job_ids:
            report.skipped_jobs.append(
                (job_id, "取数单正在计算且租约存活，受保护，本次跳过（不等待计算结束）")
            )
            continue
        selected.append(job)

    if selected:
        selected_ids = [job.id for job in selected]
        report.deleted_result_rows = (
            session.scalar(
                select(func.count())
                .select_from(db.FetchResult)
                .where(db.FetchResult.job_id.in_(selected_ids))
            )
            or 0
        )
        session.execute(
            delete(db.FetchResult).where(db.FetchResult.job_id.in_(selected_ids))
        )
        # Leases of selected (necessarily stale-lease or finished) jobs go too.
        session.execute(
            delete(db.JobLease).where(db.JobLease.job_id.in_(selected_ids))
        )
        session.execute(
            delete(db.FetchJob).where(db.FetchJob.id.in_(selected_ids))
        )
        report.deleted_job_ids = sorted(selected_ids)
    session.flush()

    # ---- 2. reap dead PENDING jobs (computing process exited) ---------------
    stale_jobs = session.scalars(
        select(db.FetchJob).where(db.FetchJob.status == "PENDING")
    ).all()
    stale_ids: list[int] = []
    for job in stale_jobs:
        if job.id in live_job_ids:
            continue  # live workers were protected in step 1 and stay pinned
        detail = (
            "计算进程已退出且心跳租约过期，清退时回收；其独占的旧输入集随之可回收"
        )
        job.status = "FAILED"
        job.status_detail = detail
        job.sample_set_id = None
        job.revision_set_id = None
        stale_ids.append(job.id)
    if stale_ids:
        session.execute(delete(db.JobLease).where(db.JobLease.job_id.in_(stale_ids)))
    report.reaped_dead_job_ids = sorted(stale_ids)
    session.flush()

    # ---- 3. reclaim unreferenced sample/revision sets -----------------------
    sample_refs, revision_refs, pending_samples, pending_revisions = (
        _referenced_set_ids(session, live_job_ids)
    )
    current = session.get(db.CurrentInput, 1)
    current_sample_id = current.sample_set_id if current else None
    current_revision_id = current.revision_set_id if current else None

    all_sample_sets = session.scalars(select(db.SampleSet.id)).all()
    all_revision_sets = session.scalars(select(db.RevisionSet.id)).all()

    sample_to_delete: list[int] = []
    for set_id in all_sample_sets:
        if set_id == current_sample_id:
            report.skipped_sample_sets.append(
                SkippedSet("sample", set_id, "当前输入，不能回收")
            )
        elif set_id in sample_refs:
            reasons = "；".join(sample_refs[set_id])
            if set_id in pending_samples:
                reasons = "计算中取数单已固定的旧输入（租约存活），" + reasons
            report.skipped_sample_sets.append(SkippedSet("sample", set_id, reasons))
        else:
            sample_to_delete.append(set_id)

    revision_to_delete: list[int] = []
    for set_id in all_revision_sets:
        if set_id == current_revision_id:
            report.skipped_revision_sets.append(
                SkippedSet("revision", set_id, "当前输入，不能回收")
            )
        elif set_id in revision_refs:
            reasons = "；".join(revision_refs[set_id])
            if set_id in pending_revisions:
                reasons = "计算中取数单已固定的旧输入（租约存活），" + reasons
            report.skipped_revision_sets.append(SkippedSet("revision", set_id, reasons))
        else:
            revision_to_delete.append(set_id)

    report.deleted_sample_rows = _delete_set_and_rows(
        session,
        db.SampleSet,
        db.Sample,
        db.Sample.sample_set_id,
        sample_to_delete,
    )
    report.deleted_sample_set_ids = sorted(sample_to_delete)
    report.deleted_revision_rows = _delete_set_and_rows(
        session,
        db.RevisionSet,
        db.Revision,
        db.Revision.revision_set_id,
        revision_to_delete,
    )
    report.deleted_revision_set_ids = sorted(revision_to_delete)
    session.flush()
    return report
