"""Point-in-time temperature reconstruction (pure, I/O free).

Rules implemented
-----------------
1. For every reading, keep only revisions whose ``known_time`` is no later
   than the prediction time; the latest such revision is the state visible at
   prediction time.  If that revision revokes the reading, the reading is
   dead -- older revisions are *not* rolled back to.
2. Among a device's readings that are currently alive and whose
   ``measurement_time`` is no later than the prediction time, pick the one
   with the greatest measurement time; ties are broken by the
   lexicographically smallest reading id.
3. When no reading qualifies, the temperature is left empty.

The attached source is the revision id (``<reading_id>#<known_time>``) of the
revision that produced the chosen reading's current state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .parsing import RevisionRow, SampleRow


@dataclass(frozen=True)
class ReadingState:
    """Visible state of one reading at a specific prediction time."""

    reading_id: str
    device_id: str
    measurement_time: datetime
    temperature: float
    source_revision_id: str
    revision_known_time: datetime


def build_reading_states(
    revisions: list[RevisionRow], prediction_time: datetime
) -> dict[str, ReadingState]:
    """Reconstruct every reading's state visible at ``prediction_time``.

    Revoked latest revisions make the reading disappear entirely.
    """
    # latest revision (greatest known_time <= prediction_time) per reading
    latest: dict[str, RevisionRow] = {}
    for rev in revisions:
        if rev.known_time > prediction_time:
            continue
        current = latest.get(rev.reading_id)
        if current is None or rev.known_time > current.known_time:
            latest[rev.reading_id] = rev

    states: dict[str, ReadingState] = {}
    for reading_id, rev in latest.items():
        if rev.revoked:
            continue  # revocation wins; do not fall back to older revisions
        assert rev.temperature is not None
        states[reading_id] = ReadingState(
            reading_id=reading_id,
            device_id=rev.device_id,
            measurement_time=rev.measurement_time,
            temperature=rev.temperature,
            source_revision_id=revision_id(rev),
            revision_known_time=rev.known_time,
        )
    return states


def resolve_all(
    samples: list[SampleRow], revisions: list[RevisionRow]
) -> list[tuple[SampleRow, float | None, str | None]]:
    """Resolve many samples, sharing reconstructed states per prediction time."""
    state_cache: dict[datetime, dict[str, ReadingState]] = {}
    results = []
    for sample in samples:
        states = state_cache.get(sample.prediction_time)
        if states is None:
            states = build_reading_states(revisions, sample.prediction_time)
            state_cache[sample.prediction_time] = states
        results.append((sample, *_pick(states, sample)))
    return results


def _pick(
    states: dict[str, ReadingState], sample: SampleRow
) -> tuple[float | None, str | None]:
    candidates = [
        state
        for state in states.values()
        if state.device_id == sample.device_id
        and state.measurement_time <= sample.prediction_time
    ]
    if not candidates:
        return None, None
    # latest measurement first; tie -> lexicographically smallest reading id
    candidates.sort(key=lambda s: (-s.measurement_time.timestamp(), s.reading_id))
    chosen = candidates[0]
    return chosen.temperature, chosen.source_revision_id


def resolve_sample(
    sample: SampleRow, revisions: list[RevisionRow]
) -> tuple[float | None, str | None]:
    """Return ``(temperature, source_revision_id)`` for one sample."""
    states = build_reading_states(revisions, sample.prediction_time)
    return _pick(states, sample)


def revision_id(rev: RevisionRow) -> str:
    """Stable identifier of a revision row, also used as the source label."""
    return f"{rev.reading_id}#{rev.known_time.isoformat()}"
