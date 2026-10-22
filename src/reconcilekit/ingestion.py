"""File-level ingestion: bytes -> a frame of raw string values.

Every value is read as text so type inference can never strip leading zeros
from identifiers or perturb monetary precision. Problems that make the file
unreadable as a whole raise :class:`FileValidationError` and block the run.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass

import pandas as pd

from reconcilekit.models import SourceInfo, SourceRow

INVOICE_COLUMNS: tuple[str, ...] = (
    "invoice_id",
    "customer_id",
    "invoice_date",
    "due_date",
    "currency",
    "invoice_amount",
)
PAYMENT_COLUMNS: tuple[str, ...] = (
    "payment_id",
    "invoice_id",
    "customer_id",
    "payment_date",
    "currency",
    "payment_amount",
)
COLUMNS = {"invoices": INVOICE_COLUMNS, "payments": PAYMENT_COLUMNS}
RECORD_PREFIX = {"invoices": "INV-R", "payments": "PAY-R"}

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_DATA_ROWS = 50_000


class FileValidationError(Exception):
    """A file-level problem that prevents a reconciliation run."""

    def __init__(self, problems: dict[str, list[str]]):
        self.problems = problems  # file kind -> actionable messages
        super().__init__(
            " | ".join(f"{kind}: {'; '.join(msgs)}" for kind, msgs in problems.items())
        )


@dataclass(frozen=True)
class ParsedFile:
    info: SourceInfo
    rows: tuple[SourceRow, ...]
    frame: pd.DataFrame  # contract columns as str, plus record_id and source_row


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_label(filename: str | None, fallback: str) -> str:
    """Reduce an uploaded filename to a harmless display label.

    Directory components are discarded and only a conservative character set is
    kept, so the label can never act as a path or inject markup.
    """
    if not filename:
        return fallback
    base = re.split(r"[\\/]", filename)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .")
    cleaned = re.sub(r"_+", "_", cleaned)[:80]
    return cleaned or fallback


def _decode(data: bytes, kind: str) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise FileValidationError(
            {kind: ["File appears to be UTF-16 encoded. Re-save it as UTF-8 CSV and upload again."]}
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FileValidationError(
            {
                kind: [
                    f"File is not valid UTF-8 (invalid byte at position {exc.start}). "
                    "Re-save it as 'CSV UTF-8' and upload again."
                ]
            }
        ) from exc
    if "\x00" in text:
        raise FileValidationError({kind: ["File contains NUL bytes; it does not look like a CSV."]})
    return text


def parse_csv(data: bytes, kind: str, label: str) -> ParsedFile:
    """Parse CSV bytes for ``kind`` ('invoices' or 'payments') under the strict contract."""
    expected = COLUMNS[kind]
    if len(data) > MAX_FILE_BYTES:
        raise FileValidationError(
            {kind: [f"File is larger than the {MAX_FILE_BYTES // (1024 * 1024)} MB limit."]}
        )
    if not data.strip():
        raise FileValidationError({kind: ["File is empty; a header row is required."]})

    text = _decode(data, kind)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        all_records = list(reader)
    except csv.Error as exc:
        raise FileValidationError(
            {kind: [f"Malformed CSV near line {reader.line_num}: {exc}."]}
        ) from exc

    # Number records 1-based like a spreadsheet; fully blank lines keep their number
    # (so row references still line up) but carry no data.
    records = [(n, rec) for n, rec in enumerate(all_records, start=1) if rec]
    if not records:
        raise FileValidationError({kind: ["File is empty; a header row is required."]})

    header = [h.strip() for h in records[0][1]]
    errors: list[str] = []
    dupes = sorted({h for h in header if header.count(h) > 1})
    if dupes:
        errors.append(f"Duplicate column headers: {', '.join(dupes)}.")
    missing = [c for c in expected if c not in header]
    if missing:
        errors.append(
            f"Missing required columns: {', '.join(missing)}. "
            f"Expected exactly these headers (any order): {', '.join(expected)}."
        )
    if errors:
        raise FileValidationError({kind: errors})

    data_records = records[1:]
    if len(data_records) > MAX_DATA_ROWS:
        raise FileValidationError({kind: [f"File has more than {MAX_DATA_ROWS:,} data rows."]})

    bad_width = [(n, len(rec)) for n, rec in data_records if len(rec) != len(header)]
    if bad_width:
        shown = ", ".join(f"row {r} has {n} fields" for r, n in bad_width[:5])
        more = f" (and {len(bad_width) - 5} more)" if len(bad_width) > 5 else ""
        raise FileValidationError(
            {kind: [f"Malformed CSV: header has {len(header)} fields but {shown}{more}."]}
        )

    positions = {c: header.index(c) for c in expected}
    extra = tuple(h for h in header if h not in expected)
    prefix = RECORD_PREFIX[kind]
    rows: list[SourceRow] = []
    for source_row, rec in data_records:
        raw = {c: rec[positions[c]] for c in expected}
        rows.append(SourceRow(f"{prefix}{source_row:05d}", kind, source_row, raw))

    frame = pd.DataFrame(
        {
            "record_id": [r.record_id for r in rows],
            "source_row": [r.source_row for r in rows],
            **{c: [r.raw[c] for r in rows] for c in expected},
        }
    )
    frame = frame.astype({c: str for c in ("record_id", *expected)})
    info = SourceInfo(kind, label, sha256_hex(data), len(rows), extra)
    return ParsedFile(info, tuple(rows), frame)
