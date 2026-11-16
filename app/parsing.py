"""CSV parsing and strict validation for the two input kinds.

Both files are UTF-8 (BOM tolerated) CSVs with a header row.  Every cell is
stripped of surrounding whitespace.  Timestamps must be ISO-8601 with an
explicit timezone (e.g. ``2026-01-02T03:04:05Z`` or ``...+08:00``); they are
normalised to timezone-aware UTC datetimes.

A rejected file raises :class:`CSVValidationError` with a human-readable
Chinese message.  The caller is responsible for discarding all partial data
so the previously accepted input stays untouched.
"""
from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import datetime, timezone

MAX_SAMPLE_ROWS = 1000
MAX_REVISION_ROWS = 10_000
MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MiB hard cap on upload size

SAMPLE_COLUMNS = ("sample_id", "device_id", "prediction_time")
REVISION_COLUMNS = (
    "reading_id",
    "device_id",
    "measurement_time",
    "known_time",
    "temperature",
    "revoked",
)

_TRUE_TOKENS = {"true", "1", "yes", "y"}
_FALSE_TOKENS = {"false", "0", "no", "n"}


class CSVValidationError(ValueError):
    """Raised when an uploaded file cannot be accepted."""


@dataclass(frozen=True)
class SampleRow:
    sample_id: str
    device_id: str
    prediction_time: datetime  # tz-aware UTC


@dataclass(frozen=True)
class RevisionRow:
    reading_id: str
    device_id: str
    measurement_time: datetime  # tz-aware UTC
    known_time: datetime       # tz-aware UTC
    temperature: float | None  # None when the revision is a revocation
    revoked: bool


def _decode(data: bytes) -> str:
    if len(data) > MAX_FILE_BYTES:
        raise CSVValidationError(
            f"文件过大：上限 {MAX_FILE_BYTES // (1024 * 1024)} MiB，"
            f"实际 {len(data)} 字节"
        )
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CSVValidationError("文件不是合法的 UTF-8 文本（CSV 需为 UTF-8 编码）") from exc


def _read_table(data: bytes, expected_columns: tuple[str, ...]) -> list[dict[str, str]]:
    text = _decode(data)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise CSVValidationError("文件为空：缺少表头行") from exc
    header = [cell.strip() for cell in header]
    if sorted(header) != sorted(expected_columns) or len(set(header)) != len(header):
        raise CSVValidationError(
            "表头不合法。\n"
            f"需要的列：{', '.join(expected_columns)}\n"
            f"实际的列：{', '.join(header) if header else '（空）'}"
        )
    rows: list[dict[str, str]] = []
    for line_no, raw in enumerate(reader, start=2):
        if not raw or all(cell.strip() == "" for cell in raw):
            continue  # tolerate trailing blank lines
        if len(raw) != len(header):
            raise CSVValidationError(
                f"第 {line_no} 行的列数为 {len(raw)}，与表头列数 {len(header)} 不一致"
            )
        rows.append({col: cell.strip() for col, cell in zip(header, raw)})
    if not rows:
        raise CSVValidationError("文件不含任何数据行")
    return rows


def _require_cell(row: dict[str, str], column: str, line_no: int) -> str:
    value = row.get(column, "")
    if value == "":
        raise CSVValidationError(f"第 {line_no} 行的列 {column} 不允许为空")
    return value


def _parse_utc(value: str, column: str, line_no: int) -> datetime:
    # datetime.fromisoformat accepts "Z" only on 3.11+, normalise for clarity.
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise CSVValidationError(
            f"第 {line_no} 行的列 {column} 不是合法 ISO-8601 时间：{value!r}"
        ) from exc
    if dt.tzinfo is None:
        raise CSVValidationError(
            f"第 {line_no} 行的列 {column} 缺少时区信息（所有时间均为带时区的 UTC 时间）："
            f"{value!r}"
        )
    return dt.astimezone(timezone.utc)


def _parse_revoked(value: str, line_no: int) -> bool:
    token = value.lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    raise CSVValidationError(
        f"第 {line_no} 行的列 revoked 取值非法：{value!r}，"
        f"允许值 true/false（或 1/0、yes/no）"
    )


def parse_samples(data: bytes) -> list[SampleRow]:
    """Parse and validate the sample CSV (max 1000 rows)."""
    rows = _read_table(data, SAMPLE_COLUMNS)
    if len(rows) > MAX_SAMPLE_ROWS:
        raise CSVValidationError(
            f"样本行数 {len(rows)} 超过上限 {MAX_SAMPLE_ROWS} 行"
        )
    parsed: list[SampleRow] = []
    seen: set[str] = set()
    for line_no, row in enumerate(rows, start=2):
        sample_id = _require_cell(row, "sample_id", line_no)
        if sample_id in seen:
            raise CSVValidationError(
                f"第 {line_no} 行样本号 {sample_id!r} 重复，sample_id 必须唯一"
            )
        seen.add(sample_id)
        device_id = _require_cell(row, "device_id", line_no)
        prediction_time = _parse_utc(
            _require_cell(row, "prediction_time", line_no), "prediction_time", line_no
        )
        parsed.append(SampleRow(sample_id, device_id, prediction_time))
    return parsed


def parse_revisions(data: bytes) -> list[RevisionRow]:
    """Parse and validate the reading-revision CSV (max 10000 rows)."""
    rows = _read_table(data, REVISION_COLUMNS)
    if len(rows) > MAX_REVISION_ROWS:
        raise CSVValidationError(
            f"修订行数 {len(rows)} 超过上限 {MAX_REVISION_ROWS} 行"
        )
    parsed: list[RevisionRow] = []
    known_times: set[datetime] = set()
    reading_facts: dict[str, tuple[str, datetime]] = {}
    reading_known: dict[str, set[datetime]] = {}
    for line_no, row in enumerate(rows, start=2):
        reading_id = _require_cell(row, "reading_id", line_no)
        device_id = _require_cell(row, "device_id", line_no)
        measurement_time = _parse_utc(
            _require_cell(row, "measurement_time", line_no), "measurement_time", line_no
        )
        known_time = _parse_utc(
            _require_cell(row, "known_time", line_no), "known_time", line_no
        )
        revoked = _parse_revoked(_require_cell(row, "revoked", line_no), line_no)
        temperature_raw = row["temperature"]
        if revoked:
            if temperature_raw != "":
                raise CSVValidationError(
                    f"第 {line_no} 行为撤销修订（revoked=true），温度列必须留空"
                )
            temperature = None
        else:
            try:
                temperature = float(temperature_raw)
            except ValueError as exc:
                raise CSVValidationError(
                    f"第 {line_no} 行的温度 {temperature_raw!r} 不是合法数字，"
                    "非撤销修订必须提供温度"
                ) from exc
            if not math.isfinite(temperature):
                raise CSVValidationError(
                    f"第 {line_no} 行的温度 {temperature_raw!r} 必须是有限数字"
                    "（不接受 NaN/Infinity）"
                )
        # 获知时刻全局唯一
        if known_time in known_times:
            raise CSVValidationError(
                f"第 {line_no} 行获知时刻 {known_time.isoformat()} 与其他修订重复，"
                "known_time 必须全局唯一"
            )
        known_times.add(known_time)
        # 同读数的设备与测量时刻固定
        fact = (device_id, measurement_time)
        if reading_id in reading_facts and reading_facts[reading_id] != fact:
            raise CSVValidationError(
                f"第 {line_no} 行读数号 {reading_id!r} 的设备号/测量时刻与此前修订不一致，"
                "同一读数号的 device_id 与 measurement_time 必须固定"
            )
        reading_facts[reading_id] = fact
        # 同读数同获知时刻不允许两条修订（已知全局唯一，这里是双保险）
        if known_time in reading_known.setdefault(reading_id, set()):
            raise CSVValidationError(
                f"第 {line_no} 行读数号 {reading_id!r} 在同一获知时刻存在多条修订"
            )
        reading_known[reading_id].add(known_time)
        parsed.append(
            RevisionRow(reading_id, device_id, measurement_time, known_time,
                        temperature, revoked)
        )
    return parsed
