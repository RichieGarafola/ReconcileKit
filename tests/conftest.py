from __future__ import annotations

import csv
import io
from datetime import date

import pytest

from reconcilekit.reconciliation import ReconciliationRun, reconcile_files

AS_OF = date(2026, 3, 31)
INV_HEADER = ["invoice_id", "customer_id", "invoice_date", "due_date", "currency", "invoice_amount"]
PAY_HEADER = [
    "payment_id",
    "invoice_id",
    "customer_id",
    "payment_date",
    "currency",
    "payment_amount",
]


def csv_bytes(header: list[str], rows: list[list[str]], encoding: str = "utf-8") -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode(encoding)


def inv(
    invoice_id: str = "INV-1",
    customer_id: str = "C1",
    invoice_date: str = "2026-01-01",
    due_date: str = "2026-01-31",
    currency: str = "USD",
    amount: str = "100.00",
) -> list[str]:
    return [invoice_id, customer_id, invoice_date, due_date, currency, amount]


def pay(
    payment_id: str = "P-1",
    invoice_id: str = "INV-1",
    customer_id: str = "C1",
    payment_date: str = "2026-01-15",
    currency: str = "USD",
    amount: str = "100.00",
) -> list[str]:
    return [payment_id, invoice_id, customer_id, payment_date, currency, amount]


def run_rows(
    invoices: list[list[str]], payments: list[list[str]], as_of: date = AS_OF
) -> ReconciliationRun:
    return reconcile_files(csv_bytes(INV_HEADER, invoices), csv_bytes(PAY_HEADER, payments), as_of)


def payment_by_id(run: ReconciliationRun, payment_id: str):
    (result,) = [p for p in run.payment_results if p.payment_id == payment_id]
    return result


def invoice_by_id(run: ReconciliationRun, invoice_id: str):
    (result,) = [i for i in run.invoice_results if i.invoice_id == invoice_id]
    return result


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.duckdb"
