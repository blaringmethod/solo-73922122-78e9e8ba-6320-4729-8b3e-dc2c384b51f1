"""Tests for CSV parsing and strict validation."""
import pytest

from app.parsing import CSVValidationError, parse_revisions, parse_samples


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


VALID_SAMPLES = (
    "sample_id,device_id,prediction_time\n"
    "S1,D1,2026-01-15T10:20:00Z\n"
    "S2,D2,2026-01-15T11:00:00+08:00\n"
)


def test_samples_happy_path_and_timezone_normalisation():
    rows = parse_samples(_csv(VALID_SAMPLES))
    assert len(rows) == 2
    assert rows[0].sample_id == "S1"
    assert rows[1].prediction_time.utcoffset().total_seconds() == 0
    # +08:00 11:00 == 03:00 UTC
    assert rows[1].prediction_time.hour == 3


def test_samples_missing_timezone_rejected():
    bad = "sample_id,device_id,prediction_time\nS1,D1,2026-01-15 10:00:00\n"
    with pytest.raises(CSVValidationError, match="时区"):
        parse_samples(_csv(bad))


def test_samples_duplicate_id_rejected():
    bad = (
        "sample_id,device_id,prediction_time\n"
        "S1,D1,2026-01-15T10:00:00Z\n"
        "S1,D2,2026-01-15T11:00:00Z\n"
    )
    with pytest.raises(CSVValidationError, match="重复"):
        parse_samples(_csv(bad))


def test_wrong_header_rejected():
    bad = "sample_id,device_id\nS1,D1\n"
    with pytest.raises(CSVValidationError, match="表头"):
        parse_samples(_csv(bad))


def test_row_limit_enforced():
    body = "sample_id,device_id,prediction_time\n" + "".join(
        f"S{i},D,2026-01-01T00:00:00Z\n" for i in range(1001)
    )
    with pytest.raises(CSVValidationError, match="上限"):
        parse_samples(_csv(body))


def test_empty_cell_rejected():
    bad = "sample_id,device_id,prediction_time\nS1,,2026-01-15T10:00:00Z\n"
    with pytest.raises(CSVValidationError, match="device_id"):
        parse_samples(_csv(bad))


VALID_REVISIONS = (
    "reading_id,device_id,measurement_time,known_time,temperature,revoked\n"
    "R1,D1,2026-01-15T10:00:00Z,2026-01-15T09:00:00Z,20.0,false\n"
    "R1,D1,2026-01-15T10:00:00Z,2026-01-15T11:00:00Z,21.0,false\n"
)


def test_revisions_happy_path():
    rows = parse_revisions(_csv(VALID_REVISIONS))
    assert len(rows) == 2
    assert rows[1].temperature == 21.0
    assert rows[1].revoked is False


def test_revoked_row_must_have_empty_temperature():
    bad = (
        "reading_id,device_id,measurement_time,known_time,temperature,revoked\n"
        "R1,D1,2026-01-15T10:00:00Z,2026-01-15T09:00:00Z,12.0,true\n"
    )
    with pytest.raises(CSVValidationError, match="撤销"):
        parse_revisions(_csv(bad))


def test_non_revoked_row_requires_temperature():
    bad = (
        "reading_id,device_id,measurement_time,known_time,temperature,revoked\n"
        "R1,D1,2026-01-15T10:00:00Z,2026-01-15T09:00:00Z,,false\n"
    )
    with pytest.raises(CSVValidationError, match="温度"):
        parse_revisions(_csv(bad))


def test_duplicate_known_time_rejected():
    bad = (
        "reading_id,device_id,measurement_time,known_time,temperature,revoked\n"
        "R1,D1,2026-01-15T10:00:00Z,2026-01-15T09:00:00Z,1.0,false\n"
        "R2,D1,2026-01-15T10:05:00Z,2026-01-15T09:00:00Z,2.0,false\n"
    )
    with pytest.raises(CSVValidationError, match="known_time"):
        parse_revisions(_csv(bad))


def test_reading_identity_must_be_fixed():
    bad = (
        "reading_id,device_id,measurement_time,known_time,temperature,revoked\n"
        "R1,D1,2026-01-15T10:00:00Z,2026-01-15T09:00:00Z,1.0,false\n"
        "R1,D9,2026-01-15T10:00:00Z,2026-01-15T11:00:00Z,2.0,false\n"
    )
    with pytest.raises(CSVValidationError, match="固定"):
        parse_revisions(_csv(bad))


def test_non_finite_temperature_rejected():
    for token in ("NaN", "Infinity", "-inf"):
        bad = (
            "reading_id,device_id,measurement_time,known_time,temperature,revoked\n"
            f"R1,D1,2026-01-15T10:00:00Z,2026-01-15T09:00:00Z,{token},false\n"
        )
        with pytest.raises(CSVValidationError, match="有限数字"):
            parse_revisions(_csv(bad))


def test_revision_row_limit_enforced():
    body = (
        "reading_id,device_id,measurement_time,known_time,temperature,revoked\n"
        + "".join(
            f"R{i},D,2026-01-01T00:00:00Z,2026-01-01T00:0{i%10}:00Z,1.0,false\n"
            for i in range(10_001)
        )
    )
    with pytest.raises(CSVValidationError, match="上限"):
        parse_revisions(_csv(body))


def test_bom_and_crlf_are_tolerated():
    body = (
        "sample_id,device_id,prediction_time\r\n"
        "S1,D1,2026-01-15T10:00:00Z\r\n"
    ).encode("utf-8-sig")
    rows = parse_samples(body)
    assert rows[0].sample_id == "S1"


def test_bad_encoding_rejected():
    with pytest.raises(CSVValidationError, match="UTF-8"):
        parse_samples("￿e".encode("utf-16"))
