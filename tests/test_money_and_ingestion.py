from __future__ import annotations

import pytest

from conftest import INV_HEADER, PAY_HEADER, csv_bytes, inv, pay
from reconcilekit.ingestion import FileValidationError, parse_csv, safe_label
from reconcilekit.money import AmountError, format_cents, parse_amount_cents
from reconcilekit.reconciliation import parse_inputs


@pytest.mark.parametrize(
    ("raw", "cents"),
    [
        ("100", 10000),
        ("100.5", 10050),
        ("100.50", 10050),
        ("0.01", 1),
        (" 19.99 ", 1999),
        ("0.10", 10),
        ("999999999999.99", 99_999_999_999_999),
    ],
)
def test_parse_amount_exact_cents(raw, cents):
    assert parse_amount_cents(raw) == cents


def test_decimal_sum_is_exact_where_float_is_not():
    # 0.1 + 0.2 != 0.3 in binary floating point; integer cents are exact.
    assert parse_amount_cents("0.10") + parse_amount_cents("0.20") == parse_amount_cents("0.30")


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("", "blank"),
        ("   ", "blank"),
        ("100.001", "more than two decimal places"),
        ("99.999", "more than two decimal places"),
        ("1e3", "exponent"),
        ("1.2E3", "exponent"),
        ("NaN", "finite"),
        ("inf", "finite"),
        ("-5.00", "negative"),
        ("0", "zero"),
        ("0.00", "zero"),
        ("$100.00", "plain decimal"),
        ("1,000.00", "plain decimal"),
        ("+100", "plain decimal"),
        ("100.", "plain decimal"),
        (".50", "plain decimal"),
        ("1 000", "plain decimal"),
        ("1000000000000.00", "maximum"),
    ],
)
def test_parse_amount_rejects(raw, fragment):
    with pytest.raises(AmountError, match=fragment):
        parse_amount_cents(raw)


def test_format_cents_two_places():
    assert format_cents(5) == "0.05"
    assert format_cents(123400) == "1234.00"
    assert format_cents(-133500) == "-1335.00"


def test_leading_zeros_and_raw_values_preserved():
    parsed = parse_csv(csv_bytes(INV_HEADER, [inv("000123", "00042")]), "invoices", "x.csv")
    assert parsed.rows[0].raw["invoice_id"] == "000123"
    assert parsed.rows[0].raw["customer_id"] == "00042"
    assert parsed.rows[0].raw["invoice_amount"] == "100.00"


def test_utf8_bom_is_accepted():
    data = b"\xef\xbb\xbf" + csv_bytes(INV_HEADER, [inv()])
    assert parse_csv(data, "invoices", "x").rows[0].raw["invoice_id"] == "INV-1"


def test_non_utf8_encoding_rejected_with_actionable_message():
    data = csv_bytes(INV_HEADER, [inv(customer_id="Café")], encoding="cp1252")
    with pytest.raises(FileValidationError) as exc:
        parse_csv(data, "invoices", "x")
    assert "UTF-8" in exc.value.problems["invoices"][0]


def test_utf16_rejected():
    data = csv_bytes(INV_HEADER, [inv()], encoding="utf-16")
    with pytest.raises(FileValidationError, match="UTF-16"):
        parse_csv(data, "invoices", "x")


def test_missing_and_duplicate_columns_rejected():
    with pytest.raises(FileValidationError, match="Missing required columns: currency"):
        parse_csv(b"invoice_id,customer_id,invoice_date,due_date,invoice_amount\n", "invoices", "x")
    header = ",".join(INV_HEADER) + ",invoice_amount\n"
    with pytest.raises(FileValidationError, match="Duplicate column headers: invoice_amount"):
        parse_csv(header.encode(), "invoices", "x")


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"   \n",
        (",".join(INV_HEADER) + "\nINV-1,C1,2026-01-01\n").encode(),  # too few fields
        (",".join(INV_HEADER) + '\n"INV-1,C1,2026-01-01,2026-01-31,USD,1\n').encode(),  # quote
        (",".join(INV_HEADER) + "\nINV\x00-1,C1,2026-01-01,2026-01-31,USD,1\n").encode(),
    ],
)
def test_malformed_files_are_file_level_errors(data):
    with pytest.raises(FileValidationError):
        parse_csv(data, "invoices", "x")


def test_both_files_reported_together():
    with pytest.raises(FileValidationError) as exc:
        parse_inputs(b"", b"nope\n")
    assert set(exc.value.problems) == {"invoices", "payments"}


def test_extra_columns_ignored_and_reported():
    data = csv_bytes([*PAY_HEADER, "memo"], [[*pay(), "=cmd()"]])
    parsed = parse_csv(data, "payments", "x")
    assert parsed.info.extra_columns == ("memo",)
    assert "memo" not in parsed.rows[0].raw


def test_column_order_is_free_and_headers_trimmed():
    header = [" payment_amount", *PAY_HEADER[:-1]]
    row = ["5.00", *pay()[:-1]]
    parsed = parse_csv(csv_bytes(header, [row]), "payments", "x")
    assert parsed.rows[0].raw["payment_amount"] == "5.00"


def test_source_rows_match_spreadsheet_numbers_with_blank_lines():
    data = ",".join(INV_HEADER) + "\n" + ",".join(inv("A")) + "\n\n" + ",".join(inv("B")) + "\n"
    parsed = parse_csv(data.encode(), "invoices", "x")
    assert [r.source_row for r in parsed.rows] == [2, 4]
    assert [r.record_id for r in parsed.rows] == ["INV-R00002", "INV-R00004"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\x\\invoices.csv", "invoices.csv"),
        ("<script>.csv", "_script_.csv"),
        ("", "fallback.csv"),
        ("..", "fallback.csv"),
    ],
)
def test_safe_label(name, expected):
    assert safe_label(name, "fallback.csv") == expected
