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
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    String,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.dialects.sqlite import DATETIME as SQLITE_DATETIME
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.types import TypeDecorator

from .parsing import RevisionRow, SampleRow


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
    id: Mapped[int] = mapped_column(primary_key=True)
    sample_set_id: Mapped[int] = mapped_column(ForeignKey("sample_sets.id", ondelete="RESTRICT"))
    revision_set_id: Mapped[int] = mapped_column(
        ForeignKey("revision_sets.id", ondelete="RESTRICT")
    )
    total_samples: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(TS, default=lambda: datetime.now(timezone.utc))
    results: Mapped[list["FetchResult"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


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


def init_db() -> None:
    Base.metadata.create_all(engine)


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


def _get_current(session) -> CurrentInput:
    current = session.get(CurrentInput, 1)
    if current is None:
        current = CurrentInput(id=1, sample_set_id=None, revision_set_id=None)
        session.add(current)
        session.flush()
    return current


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


def create_fetch_job(session) -> FetchJob:
    """Snapshot current inputs and materialise all result rows.

    Runs inside the caller's single short transaction.  Every result is
    computed and inserted here; afterwards pages only read stored rows, so
    consistency no longer depends on any open transaction.
    """
    from .logic import resolve_all  # local import keeps module import light

    overview = get_current_overview(session)
    if not overview["ready"]:
        raise ValueError("两份输入（样本 CSV 与读数修订 CSV）均已成功上传后才能发起取数")
    sample_set: SampleSet = overview["sample_set"]
    revision_set: RevisionSet = overview["revision_set"]

    revisions = load_revisions(session, revision_set.id)
    samples = session.scalars(
        select(Sample)
        .where(Sample.sample_set_id == sample_set.id)
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

    job = FetchJob(
        sample_set_id=sample_set.id,
        revision_set_id=revision_set.id,
        total_samples=len(sample_rows),
    )
    session.add(job)
    session.flush()

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
    session.flush()
    return job


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
