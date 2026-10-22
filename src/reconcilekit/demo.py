"""Bundled synthetic demonstration data (fictional; no real people or businesses).

The CSVs ship inside the package (``reconcilekit/demo_data``) and are read with
``importlib.resources``, so the demo works from a source checkout and from an
installed wheel alike. They are the single authoritative copy of the demo data.
"""

from __future__ import annotations

from datetime import date
from importlib import resources

# Fixed as-of date so demo results never depend on the machine clock.
DEMO_AS_OF = date(2026, 3, 31)
DEMO_INVOICES_NAME = "invoices.csv"
DEMO_PAYMENTS_NAME = "payments.csv"


def _demo_file(name: str) -> bytes:
    return resources.files("reconcilekit.demo_data").joinpath(name).read_bytes()


def demo_invoices() -> bytes:
    return _demo_file(DEMO_INVOICES_NAME)


def demo_payments() -> bytes:
    return _demo_file(DEMO_PAYMENTS_NAME)


def load_demo() -> tuple[bytes, bytes]:
    """Return (invoice_bytes, payment_bytes) of the bundled demo."""
    return demo_invoices(), demo_payments()
