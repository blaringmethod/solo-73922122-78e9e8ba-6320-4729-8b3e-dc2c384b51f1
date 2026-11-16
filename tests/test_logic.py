"""Tests for the point-in-time reconstruction rules."""
from datetime import datetime, timezone

from app.logic import resolve_sample
from app.parsing import RevisionRow, SampleRow


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def rev(reading_id, device_id, measured, known, temperature=None, revoked=False):
    return RevisionRow(
        reading_id=reading_id,
        device_id=device_id,
        measurement_time=dt(measured),
        known_time=dt(known),
        temperature=None if revoked else temperature,
        revoked=revoked,
    )


def sample(device_id="D1", when="2026-01-15T12:00:00Z"):
    return SampleRow(sample_id="S1", device_id=device_id, prediction_time=dt(when))


def test_uses_latest_known_revision_only():
    # R1 corrected from 20.0 to 21.0; correction known before prediction.
    revs = [
        rev("R1", "D1", "2026-01-15T10:00:00Z", "2026-01-15T09:00:00Z", 20.0),
        rev("R1", "D1", "2026-01-15T10:00:00Z", "2026-01-15T11:00:00Z", 21.0),
    ]
    temp, source = resolve_sample(sample(), revs)
    assert temp == 21.0
    assert source == "R1#2026-01-15T11:00:00+00:00"


def test_future_known_revision_is_invisible():
    # Correction arrives after the prediction: the old value must be used.
    revs = [
        rev("R1", "D1", "2026-01-15T10:00:00Z", "2026-01-15T09:00:00Z", 20.0),
        rev("R1", "D1", "2026-01-15T10:00:00Z", "2026-01-15T13:00:00Z", 21.0),
    ]
    temp, source = resolve_sample(sample(), revs)
    assert temp == 20.0
    assert source == "R1#2026-01-15T09:00:00+00:00"


def test_revocation_does_not_roll_back():
    # Latest visible revision is a revocation -> reading disappears,
    # even though an older revision carried a temperature.
    revs = [
        rev("R1", "D1", "2026-01-15T10:00:00Z", "2026-01-15T09:00:00Z", 20.0),
        rev("R1", "D1", "2026-01-15T10:00:00Z", "2026-01-15T11:30:00Z", revoked=True),
    ]
    temp, source = resolve_sample(sample(), revs)
    assert temp is None and source is None


def test_revocation_arriving_after_prediction_is_invisible():
    revs = [
        rev("R1", "D1", "2026-01-15T10:00:00Z", "2026-01-15T09:00:00Z", 20.0),
        rev("R1", "D1", "2026-01-15T10:00:00Z", "2026-01-15T13:00:00Z", revoked=True),
    ]
    temp, _ = resolve_sample(sample(), revs)
    assert temp == 20.0


def test_latest_measurement_wins_among_alive_readings():
    revs = [
        rev("R1", "D1", "2026-01-15T10:00:00Z", "2026-01-15T10:00:00Z", 20.0),
        rev("R2", "D1", "2026-01-15T11:00:00Z", "2026-01-15T11:00:00Z", 22.0),
        # R3 measured later but revoked -> excluded
        rev("R3", "D1", "2026-01-15T11:30:00Z", "2026-01-15T11:30:00Z", revoked=True),
    ]
    temp, source = resolve_sample(sample(), revs)
    assert temp == 22.0
    assert source.startswith("R2#")


def test_measurements_after_prediction_are_excluded():
    revs = [
        rev("R1", "D1", "2026-01-15T10:00:00Z", "2026-01-15T09:30:00Z", 20.0),
        rev("R2", "D1", "2026-01-15T13:00:00Z", "2026-01-15T11:00:00Z", 30.0),
    ]
    temp, _ = resolve_sample(sample(when="2026-01-15T12:00:00Z"), revs)
    assert temp == 20.0


def test_same_measurement_time_picks_smallest_reading_id():
    revs = [
        rev("R5", "D1", "2026-01-15T10:30:00Z", "2026-01-15T10:30:00Z", 99.0),
        rev("R2", "D1", "2026-01-15T10:30:00Z", "2026-01-15T10:31:00Z", 22.0),
    ]
    temp, source = resolve_sample(sample(), revs)
    assert temp == 22.0
    assert source.startswith("R2#")


def test_no_candidate_leaves_empty():
    revs = [
        # other device
        rev("R1", "D2", "2026-01-15T10:00:00Z", "2026-01-15T09:00:00Z", 20.0),
        # same device but measurement in the future
        rev("R2", "D1", "2026-01-15T13:00:00Z", "2026-01-15T11:00:00Z", 30.0),
    ]
    temp, source = resolve_sample(sample(), revs)
    assert temp is None and source is None


def test_demo_scenario_end_to_end():
    """Mirror demo/samples.csv + demo/revisions.csv expected outputs."""
    revs = [
        rev("R1", "D1", "2026-01-15T10:00:00Z", "2026-01-15T09:00:00Z", 20.0),
        rev("R1", "D1", "2026-01-15T10:00:00Z", "2026-01-15T11:00:00Z", 21.0),
        rev("R2", "D1", "2026-01-15T10:30:00Z", "2026-01-15T10:45:00Z", 22.0),
        rev("R2", "D1", "2026-01-15T10:30:00Z", "2026-01-15T12:30:00Z", revoked=True),
        rev("R5", "D1", "2026-01-15T10:30:00Z", "2026-01-15T10:50:00Z", 99.0),
        rev("R3", "D1", "2026-01-15T11:00:00Z", "2026-01-15T13:00:00Z", revoked=True),
        rev("R6", "D2", "2026-01-15T08:00:00Z", "2026-01-15T08:30:00Z", 5.0),
        rev("R7", "D2", "2026-01-15T12:00:00Z", "2026-01-15T12:10:00Z", 6.0),
    ]
    cases = {
        "S1": ("D1", "2026-01-15T10:20:00Z", 20.0, "R1#2026-01-15T09:00:00+00:00"),
        "S2": ("D1", "2026-01-15T11:30:00Z", 22.0, "R2#2026-01-15T10:45:00+00:00"),
        # R3 and R2 are revoked by 13:30; no rollback -> R5 (latest alive) wins
        "S3": ("D1", "2026-01-15T13:30:00Z", 99.0, "R5#2026-01-15T10:50:00+00:00"),
        "S4": ("D1", "2026-01-15T09:10:00Z", None, None),
        "S5": ("D2", "2026-01-15T10:00:00Z", 5.0, "R6#2026-01-15T08:30:00+00:00"),
        "S6": ("D2", "2026-01-15T07:30:00Z", None, None),
    }
    for sid, (device, when, expected_temp, expected_source) in cases.items():
        temp, source = resolve_sample(
            SampleRow(sample_id=sid, device_id=device, prediction_time=dt(when)), revs
        )
        assert (temp, source) == (expected_temp, expected_source), sid
