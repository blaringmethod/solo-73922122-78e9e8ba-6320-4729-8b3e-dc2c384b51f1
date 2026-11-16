"""Database access layer.

The schema is append-only with versioned input sets:

* uploading a file creates a brand-new set; the ``current_inputs`` single row
  only repoints to it once every row has been inserted and validated, inside
  one transaction -- a rejected upload can therefore never corrupt the old
  input;
* a 取数单 (fetch job) snapshots the current set ids at creation time and
  materialises *all* of its result rows up front, so later uploads cannot
  change an existing job and pages can be served without any long-lived
  transaction (each HTTP request uses its own short session).

Pagination is keyset pagination over ``sample_id`` (which is unique per
sample set), giving restart-safe, no-duplicate/no-gap pages for retries with
the same cursor.
"""
from __future__ import annotations

import os
import socket
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.dialects.sqlite import DATETIME as SQLITE_DATETIME
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.sql.expression import Insert
from sqlalchemy.types import TypeDecorator

from .parsing import RevisionRow, SampleRow


class _InsertIfAbsent(Insert):
    """INSERT that silently ignores a primary-key conflict.

    Compiled to ``ON CONFLICT DO NOTHING`` on PostgreSQL and
    ``INSERT OR IGNORE`` on SQLite: the two backends this app ships on.
    """

    inherit_cache = True


@compiles(_InsertIfAbsent, "postgresql")
def _compile_insert_if_absent_pg(element, compiler, **kw):
    return compiler.visit_insert(element, **kw) + " ON CONFLICT DO NOTHING"


@compiles(_InsertIfAbsent, "sqlite")
def _compile_insert_if_absent_sqlite(element, compiler, **kw):
    return compiler.visit_insert(element, **kw).replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)


class BigIntPK(BigInteger):
    """BIGINT everywhere, plain INTEGER on SQLite.

    SQLite only grants rowid autoincrement to a column with INTEGER affinity;
    a BIGINT primary key would require an explicit id on every insert.
    """


@compiles(BigIntPK, "sqlite")
def _bigint_pk_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect hook
    return "INTEGER"


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC timestamp.

    PostgreSQL stores it natively (``TIMESTAMPTZ``); SQLite has no tz support,
    so values are converted to naive UTC on the way in and re-attached to UTC
    on the way out.  Application code always works with tz-aware datetimes.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(SQLITE_DATETIME())
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("时间戳必须带时区")
        utc_value = value.astimezone(timezone.utc)
        if dialect.name == "sqlite":
            return utc_value.replace(tzinfo=None)
        return utc_value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


# Timestamp type: timezone-aware on PostgreSQL, naive UTC on SQLite (see above).
TS = UTCDateTime()


class SampleSet(Base):
    __tablename__ = "sample_sets"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(260))
    row_count: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(TS, default=lambda: datetime.now(timezone.utc))
    samples: Mapped[list["Sample"]] = relationship(
        back_populates="sample_set", cascade="all, delete-orphan"
    )


class Sample(Base):
    __tablename__ = "samples"
    __table_args__ = (UniqueConstraint("sample_set_id", "sample_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sample_set_id: Mapped[int] = mapped_column(
        ForeignKey("sample_sets.id", ondelete="CASCADE"), index=True
    )
    sample_id: Mapped[str] = mapped_column(String(200), index=True)
    device_id: Mapped[str] = mapped_column(String(200), index=True)
    prediction_time: Mapped[datetime] = mapped_column(TS, index=True)
    sample_set: Mapped[SampleSet] = relationship(back_populates="samples")


class RevisionSet(Base):
    __tablename__ = "revision_sets"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(260))
    row_count: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(TS, default=lambda: datetime.now(timezone.utc))
    revisions: Mapped[list["Revision"]] = relationship(
        back_populates="revision_set", cascade="all, delete-orphan"
    )


class Revision(Base):
    __tablename__ = "revisions"
    __table_args__ = (
        UniqueConstraint("revision_set_id", "reading_id", "known_time"),
    )
    # SQLite only auto-increments an INTEGER-affinity primary key, while
    # PostgreSQL keeps BIGINT for the high-volume revision/result tables.
    id: Mapped[int] = mapped_column(
        BigIntPK(),
        primary_key=True,
        autoincrement=True,
    )
    revision_set_id: Mapped[int] = mapped_column(
        ForeignKey("revision_sets.id", ondelete="CASCADE"), index=True
    )
    reading_id: Mapped[str] = mapped_column(String(200), index=True)
    device_id: Mapped[str] = mapped_column(String(200), index=True)
    measurement_time: Mapped[datetime] = mapped_column(TS, index=True)
    known_time: Mapped[datetime] = mapped_column(TS, index=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean)
    revision_set: Mapped[RevisionSet] = relationship(back_populates="revisions")

    @property
    def source_revision_id(self) -> str:
        return f"{self.reading_id}#{self.known_time.isoformat()}"

    def as_row(self) -> RevisionRow:
        return RevisionRow(
            reading_id=self.reading_id,
            device_id=self.device_id,
            measurement_time=self.measurement_time,
            known_time=self.known_time,
            temperature=None if self.revoked else self.temperature,
            revoked=self.revoked,
        )


class CurrentInput(Base):
    """Single-row table pointing at the currently active input sets."""
    __tablename__ = "current_inputs"
    id: Mapped[int] = mapped_column(primary_key=True)
    sample_set_id: Mapped[int | None] = mapped_column(
        ForeignKey("sample_sets.id", ondelete="RESTRICT"), nullable=True
    )
    revision_set_id: Mapped[int | None] = mapped_column(
        ForeignKey("revision_sets.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(TS, default=lambda: datetime.now(timezone.utc))


class FetchJob(Base):
    __tablename__ = "fetch_jobs"
    # PENDING: inputs pinned, materialisation in progress (a JobLease exists).
    # READY:   all result rows materialised, the immutable snapshot page.
    # FAILED:  the computing process died and a retirement reaped it; the set
    #          references are detached (nullable) so the old sets can be freed.
    id: Mapped[int] = mapped_column(primary_key=True)
    sample_set_id: Mapped[int | None] = mapped_column(
        ForeignKey("sample_sets.id", ondelete="RESTRICT"), nullable=True
    )
    revision_set_id: Mapped[int | None] = mapped_column(
        ForeignKey("revision_sets.id", ondelete="RESTRICT"), nullable=True
    )
    total_samples: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=lambda: datetime.now(timezone.utc))
    results: Mapped[list["FetchResult"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    @property
    def is_ready(self) -> bool:
        return self.status == "READY"


class JobLease(Base):
    """Heartbeat lease held while a job is being materialised.

    The row is created together with the PENDING job pin and deleted as soon
    as materialisation finishes.  While ``heartbeat_at`` is fresh the pinned
    (possibly already superseded) input sets are *exclusive* to this worker:
    retirement must skip them.  If the computing process exits, the heartbeat
    goes stale; the next retirement treats the job as dead and may free its
    sets.  Portable across PostgreSQL and SQLite (no backend-specific locks).
    """

    __tablename__ = "job_leases"
    job_id: Mapped[int] = mapped_column(
        ForeignKey("fetch_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    owner: Mapped[str] = mapped_column(String(120))
    heartbeat_at: Mapped[datetime] = mapped_column(TS, index=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=lambda: datetime.now(timezone.utc))


class FetchResult(Base):
    __tablename__ = "fetch_results"
    __table_args__ = (
        UniqueConstraint("job_id", "sample_id"),
    )
    id: Mapped[int] = mapped_column(
        BigIntPK(),
        primary_key=True,
        autoincrement=True,
    )
    job_id: Mapped[int] = mapped_column(
        ForeignKey("fetch_jobs.id", ondelete="CASCADE"), index=True
    )
    sample_id: Mapped[str] = mapped_column(String(200), index=True)
    device_id: Mapped[str] = mapped_column(String(200))
    prediction_time: Mapped[datetime] = mapped_column(TS)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_revision_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    job: Mapped[FetchJob] = relationship(back_populates="results")


def database_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://tempapp:tempapp@db:5432/tempapp",
    )


engine = create_engine(database_url(), pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _lease_ttl_seconds() -> int:
    """A lease whose heartbeat is older than this is considered dead.

    Overridable via ``JOB_LEASE_TTL_SECONDS`` so tests can simulate a crashed
    worker without sleeping for the production interval.
    """
    raw = os.environ.get("JOB_LEASE_TTL_SECONDS")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return 30


def lease_ttl_seconds() -> int:
    return _lease_ttl_seconds()


def lease_ttl() -> timedelta:
    return timedelta(seconds=_lease_ttl_seconds())


def instance_owner() -> str:
    """Stable identifier of this web process, shown on lease rows."""
    explicit = os.environ.get("INSTANCE_OWNER")
    if explicit:
        return explicit[:120]
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def init_db() -> None:
    # Two instances start together and may run DDL at the same moment.
    # PostgreSQL serialises CREATE TABLE safely; SQLite can still raise
    # "table already exists" between the checkfirst probe and the CREATE, so
    # retry once in that case (the winning instance has created the table).
    try:
        Base.metadata.create_all(engine)
    except OperationalError as exc:
        if engine.dialect.name != "sqlite" or "already exists" not in str(exc):
            raise
        Base.metadata.create_all(engine)
    _apply_lightweight_migrations()


def _apply_lightweight_migrations() -> None:
    """Add columns introduced after the first release to pre-existing tables.

    Deployments created before the retirement feature lack ``status`` etc.;
    ``create_all`` never alters existing tables, so add them idempotently.
    Existing jobs are all fully materialised snapshots, hence backfilled to
    READY.
    """
    inspector = inspect(engine)
    existing = {column["name"] for column in inspector.get_columns("fetch_jobs")}
    type_map = {
        "status": "VARCHAR(16)",
        "status_detail": "TEXT",
    }
    with engine.begin() as conn:
        for column, ddl_type in type_map.items():
            if column not in existing:
                try:
                    conn.execute(
                        text(f"ALTER TABLE fetch_jobs ADD COLUMN {column} {ddl_type}")
                    )
                except OperationalError as exc:
                    # Concurrent startup on SQLite: the other instance added it.
                    if engine.dialect.name != "sqlite" or "duplicate column" not in str(exc):
                        raise
        if "status" not in existing:
            conn.execute(text("UPDATE fetch_jobs SET status = 'READY'"))
        if "sample_set_id" in existing and engine.dialect.name == "postgresql":
            # Older PostgreSQL schema declared the snapshot FKs NOT NULL;
            # retirement of a dead job needs to detach them.  SQLite cannot
            # DROP NOT NULL via ALTER TABLE, but SQLite databases are test-only
            # and always created fresh by create_all (nullable from the start).
            conn.execute(
                text(
                    "ALTER TABLE fetch_jobs ALTER COLUMN sample_set_id DROP NOT NULL"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE fetch_jobs ALTER COLUMN revision_set_id DROP NOT NULL"
                )
            )


@contextmanager
def session_scope() -> Iterator:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _ensure_current_row(session) -> None:
    """Idempotently create the singleton current-input row (cross-process safe).

    Uses :class:`_InsertIfAbsent`, compiled to ``ON CONFLICT DO NOTHING`` on
    PostgreSQL and ``INSERT OR IGNORE`` on SQLite, so two instances hitting an
    empty database at once cannot violate the primary key.
    """
    session.execute(
        _InsertIfAbsent(CurrentInput.__table__).values(
            id=1,
            sample_set_id=None,
            revision_set_id=None,
            updated_at=datetime.now(timezone.utc),
        )
    )


def _get_current(session) -> CurrentInput:
    current = session.get(CurrentInput, 1)
    if current is None:
        _ensure_current_row(session)
        session.flush()
        current = session.get(CurrentInput, 1)
    return current


# Serialises first-time access to the current-input singleton *within* this
# process.  Cross-process safety (two web instances starting together) comes
# from the idempotent upsert in :func:`current_input_locked`, which relies on
# the database's own write/row locks instead of a Python-only mutex.
_current_input_lock = threading.Lock()


@contextmanager
def current_input_locked(session):
    """Yield the singleton current-input row, safe across processes/threads.

    The row is created with an idempotent upsert (``ON CONFLICT DO NOTHING``,
    supported by both PostgreSQL and SQLite), so two instances racing on an
    empty database can never both insert the singleton.  PostgreSQL then takes
    a ``SELECT ... FOR UPDATE`` row lock; SQLite has no row locks, but the
    upsert's write lock plus the in-process mutex already serialise writers.

    Used by startup demo loading so two instances cannot each insert the demo
    input sets.
    """
    use_process_lock = session.bind.dialect.name == "sqlite"
    if use_process_lock:
        _current_input_lock.acquire()
    try:
        _ensure_current_row(session)
        stmt = select(CurrentInput).where(CurrentInput.id == 1)
        if not use_process_lock:
            stmt = stmt.with_for_update()
        current = session.scalars(stmt).one()
        yield current
    finally:
        if use_process_lock:
            _current_input_lock.release()


def get_current_overview(session) -> dict:
    current = _get_current(session)
    sample_set = session.get(SampleSet, current.sample_set_id) if current.sample_set_id else None
    revision_set = (
        session.get(RevisionSet, current.revision_set_id) if current.revision_set_id else None
    )
    return {
        "sample_set": sample_set,
        "revision_set": revision_set,
        "ready": sample_set is not None and revision_set is not None,
    }


def replace_sample_set(session, rows: list[SampleRow], filename: str) -> SampleSet:
    """Insert a new sample set and atomically repoint the current input."""
    new_set = SampleSet(filename=filename, row_count=len(rows))
    session.add(new_set)
    session.flush()
    session.add_all(
        [
            Sample(
                sample_set_id=new_set.id,
                sample_id=row.sample_id,
                device_id=row.device_id,
                prediction_time=row.prediction_time,
            )
            for row in rows
        ]
    )
    current = _get_current(session)
    current.sample_set_id = new_set.id
    current.updated_at = datetime.now(timezone.utc)
    session.flush()
    return new_set


def replace_revision_set(
    session, rows: list[RevisionRow], filename: str
) -> RevisionSet:
    new_set = RevisionSet(filename=filename, row_count=len(rows))
    session.add(new_set)
    session.flush()
    session.add_all(
        [
            Revision(
                revision_set_id=new_set.id,
                reading_id=row.reading_id,
                device_id=row.device_id,
                measurement_time=row.measurement_time,
                known_time=row.known_time,
                temperature=row.temperature,
                revoked=row.revoked,
            )
            for row in rows
        ]
    )
    current = _get_current(session)
    current.revision_set_id = new_set.id
    current.updated_at = datetime.now(timezone.utc)
    session.flush()
    return new_set


def load_revisions(session, revision_set_id: int) -> list[RevisionRow]:
    """Load a whole revision set (max 10k rows) for in-memory resolution."""
    records = session.scalars(
        select(Revision)
        .where(Revision.revision_set_id == revision_set_id)
        .order_by(Revision.id)
    ).all()
    return [record.as_row() for record in records]


def create_fetch_job(session, owner: str | None = None) -> FetchJob:
    """Pin the current inputs into a new PENDING job and take its lease.

    Only the snapshot ids are written here; result materialisation happens in
    :func:`materialise_fetch_job` with its own short session, so an upload
    replacing the current input meanwhile cannot change this job.  The lease
    row makes the pinned -- possibly already superseded -- sets exclusive to
    the caller while computation is running.
    """
    overview = get_current_overview(session)
    if not overview["ready"]:
        raise ValueError("两份输入（样本 CSV 与读数修订 CSV）均已成功上传后才能发起取数")
    sample_set: SampleSet = overview["sample_set"]
    revision_set: RevisionSet = overview["revision_set"]

    job = FetchJob(
        sample_set_id=sample_set.id,
        revision_set_id=revision_set.id,
        total_samples=0,
        status="PENDING",
    )
    session.add(job)
    session.flush()
    now = datetime.now(timezone.utc)
    session.add(
        JobLease(
            job_id=job.id,
            owner=owner or instance_owner(),
            heartbeat_at=now,
            created_at=now,
        )
    )
    session.flush()
    return job


def refresh_job_lease(session, job_id: int) -> datetime:
    """Push the heartbeat forward; raises if the lease vanished."""
    lease = session.get(JobLease, job_id)
    if lease is None:
        raise LookupError(f"取数单 {job_id} 的计算租约已不存在")
    lease.heartbeat_at = datetime.now(timezone.utc)
    session.flush()
    return lease.heartbeat_at


def release_job_lease(session, job_id: int) -> None:
    lease = session.get(JobLease, job_id)
    if lease is not None:
        session.delete(lease)
        session.flush()


def live_lease_job_ids(session) -> set[int]:
    """Ids of jobs whose computing process is still alive (fresh heartbeat)."""
    cutoff = datetime.now(timezone.utc) - lease_ttl()
    rows = session.scalars(select(JobLease.job_id).where(JobLease.heartbeat_at >= cutoff))
    return set(rows)


def materialise_fetch_job(session, job_id: int) -> FetchJob:
    """Compute and insert every result row for a pinned PENDING job.

    Runs inside the caller's short transaction; the lease taken at pin time
    protects the snapshot sets from retirement while this runs.
    """
    from .logic import resolve_all  # local import keeps module import light

    job = session.get(FetchJob, job_id)
    if job is None:
        raise LookupError(f"取数单 {job_id} 不存在")
    if job.status != "PENDING":
        raise ValueError(f"取数单 {job_id} 不在计算中状态（当前 {job.status}）")

    revisions = load_revisions(session, job.revision_set_id)
    samples = session.scalars(
        select(Sample)
        .where(Sample.sample_set_id == job.sample_set_id)
        .order_by(Sample.id)
    ).all()
    sample_rows = [
        SampleRow(
            sample_id=sample.sample_id,
            device_id=sample.device_id,
            prediction_time=sample.prediction_time,
        )
        for sample in samples
    ]

    result_rows = []
    for sample, temperature, source in resolve_all(sample_rows, revisions):
        result_rows.append(
            FetchResult(
                job_id=job.id,
                sample_id=sample.sample_id,
                device_id=sample.device_id,
                prediction_time=sample.prediction_time,
                temperature=temperature,
                source_revision_id=source,
            )
        )
    session.add_all(result_rows)
    job.total_samples = len(sample_rows)
    job.status = "READY"
    job.status_detail = None
    session.flush()
    return job


def fail_fetch_job(session, job_id: int, detail: str) -> None:
    """Mark a job FAILED and drop its lease (if any)."""
    job = session.get(FetchJob, job_id)
    if job is None:
        return
    job.status = "FAILED"
    job.status_detail = detail
    release_job_lease(session, job_id)
    session.flush()


def fetch_job_page(
    session, job_id: int, cursor: str | None, limit: int
) -> tuple[list[FetchResult], str | None, bool]:
    """Keyset page: rows with sample_id > cursor, ordered by sample_id.

    The extra row beyond ``limit`` decides whether another page exists, so
    the same cursor always returns the same rows even across restarts.
    """
    stmt = (
        select(FetchResult)
        .where(FetchResult.job_id == job_id)
        .order_by(FetchResult.sample_id, FetchResult.id)
    )
    if cursor:
        stmt = stmt.where(FetchResult.sample_id > cursor)
    stmt = stmt.limit(limit + 1)
    rows = list(session.scalars(stmt).all())
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = page[-1].sample_id if has_more and page else None
    return page, next_cursor, has_more
