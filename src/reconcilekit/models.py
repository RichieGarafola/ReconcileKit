"""Domain vocabulary: statuses, rule codes, and immutable result records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class InvoiceStatus(StrEnum):
    UNPAID = "UNPAID"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    OVERPAID = "OVERPAID"


class PaymentStatus(StrEnum):
    APPLIED = "APPLIED"
    UNMATCHED = "UNMATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    BLOCKED = "BLOCKED"


class Disposition(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    INVESTIGATING = "INVESTIGATING"
    RESOLVED_EXTERNALLY = "RESOLVED_EXTERNALLY"


class RuleCode(StrEnum):
    # Matching outcomes
    EXACT_REFERENCE_MATCH = "EXACT_REFERENCE_MATCH"
    INVOICE_PAID = "INVOICE_PAID"
    INVOICE_UNPAID = "INVOICE_UNPAID"
    INVOICE_PARTIALLY_PAID = "INVOICE_PARTIALLY_PAID"
    INVOICE_OVERPAID = "INVOICE_OVERPAID"
    PAYMENT_REFERENCE_MISSING = "PAYMENT_REFERENCE_MISSING"
    PAYMENT_REFERENCE_NOT_FOUND = "PAYMENT_REFERENCE_NOT_FOUND"
    CUSTOMER_REFERENCE_CONFLICT = "CUSTOMER_REFERENCE_CONFLICT"
    AMBIGUOUS_INVOICE_REFERENCE = "AMBIGUOUS_INVOICE_REFERENCE"
    REFERENCED_INVOICE_INVALID = "REFERENCED_INVOICE_INVALID"
    PAYMENT_BEFORE_INVOICE_DATE = "PAYMENT_BEFORE_INVOICE_DATE"
    # Validation / quarantine
    DUPLICATE_INVOICE_IDENTITY = "DUPLICATE_INVOICE_IDENTITY"
    DUPLICATE_PAYMENT_ID = "DUPLICATE_PAYMENT_ID"
    MISSING_REQUIRED_VALUE = "MISSING_REQUIRED_VALUE"
    INVALID_AMOUNT = "INVALID_AMOUNT"
    INVALID_DATE = "INVALID_DATE"
    DUE_DATE_BEFORE_INVOICE_DATE = "DUE_DATE_BEFORE_INVOICE_DATE"
    UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"
    TRANSACTION_AFTER_AS_OF = "TRANSACTION_AFTER_AS_OF"


class ExceptionCategory(StrEnum):
    VALIDATION = "VALIDATION"
    INVOICE = "INVOICE"
    PAYMENT = "PAYMENT"


class Severity(StrEnum):
    QUARANTINE = "QUARANTINE"  # record excluded from eligible totals
    REVIEW = "REVIEW"  # eligible record whose outcome needs a human look
    INFO = "INFO"  # informational; does not change any allocation


QUARANTINED = "QUARANTINED"


@dataclass(frozen=True)
class SourceInfo:
    kind: str  # "invoices" | "payments"
    label: str  # sanitized display label (never used as a filesystem path)
    sha256: str
    row_count: int
    extra_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceRow:
    record_id: str  # e.g. INV-R0002; stable for identical input bytes
    kind: str
    source_row: int  # CSV record number, header = row 1
    raw: dict[str, str]  # contract columns exactly as read


@dataclass(frozen=True)
class ValidationIssue:
    record_id: str
    kind: str
    source_row: int
    rule_code: RuleCode
    field: str | None
    raw_value: str | None
    message: str


@dataclass(frozen=True)
class Invoice:
    record_id: str
    source_row: int
    invoice_id_raw: str
    invoice_id: str
    customer_id_raw: str
    customer_id: str
    invoice_date: date
    due_date: date
    currency: str
    amount_cents: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.customer_id, self.invoice_id)

    @property
    def normalized(self) -> bool:
        return self.invoice_id != self.invoice_id_raw or self.customer_id != self.customer_id_raw


@dataclass(frozen=True)
class Payment:
    record_id: str
    source_row: int
    payment_id_raw: str
    payment_id: str
    invoice_id_raw: str
    invoice_id: str | None  # None when the reference is blank
    customer_id_raw: str
    customer_id: str
    payment_date: date
    currency: str
    amount_cents: int

    @property
    def normalized(self) -> bool:
        return (
            self.payment_id != self.payment_id_raw
            or (self.invoice_id or "") != self.invoice_id_raw
            or self.customer_id != self.customer_id_raw
        )


@dataclass(frozen=True)
class DuplicateGroup:
    kind: str
    identity: tuple[str, ...]
    record_ids: tuple[str, ...]
    source_rows: tuple[int, ...]
    identical: bool
    differing_fields: tuple[str, ...]


@dataclass(frozen=True)
class InvoiceResult:
    record_id: str
    source_row: int
    invoice_id_raw: str
    invoice_id: str
    customer_id_raw: str
    customer_id: str
    invoice_date: date
    due_date: date
    invoice_amount_cents: int
    applied_payment_cents: int
    outstanding_cents: int
    overpayment_cents: int
    status: InvoiceStatus
    rule_code: RuleCode
    is_overdue: bool
    days_overdue: int
    payment_record_ids: tuple[str, ...]
    normalization_applied: bool
    explanation: str


@dataclass(frozen=True)
class PaymentResult:
    record_id: str
    source_row: int
    payment_id_raw: str
    payment_id: str
    invoice_id_raw: str
    invoice_id: str | None
    customer_id_raw: str
    customer_id: str
    payment_date: date
    payment_amount_cents: int
    status: PaymentStatus
    rule_code: RuleCode
    matched_invoice_record_id: str | None
    related_invoice_record_ids: tuple[str, ...]
    warnings: tuple[RuleCode, ...]
    normalization_applied: bool
    explanation: str


@dataclass(frozen=True)
class ExceptionRecord:
    exception_id: str
    category: ExceptionCategory
    rule_code: RuleCode
    severity: Severity
    classification: str  # the automated outcome, e.g. UNMATCHED or QUARANTINED
    customer_id: str | None
    invoice_id: str | None
    payment_id: str | None
    record_ids: tuple[str, ...]
    source_refs: str  # human-readable, e.g. "invoices row 10, 11"
    amount_cents: int | None
    explanation: str


@dataclass(frozen=True)
class Summary:
    # Monetary totals over ELIGIBLE records only (integer cents).
    eligible_invoice_cents: int  # I
    applied_payment_cents: int  # A
    outstanding_cents: int  # O
    overpayment_cents: int  # X
    eligible_payment_cents: int  # P
    unmatched_payment_cents: int  # U
    ambiguous_payment_cents: int  # M
    blocked_payment_cents: int  # B
    overdue_cents: int
    # Counts
    invoice_rows: int
    payment_rows: int
    eligible_invoices: int
    eligible_payments: int
    quarantined_invoice_rows: int
    quarantined_payment_rows: int
    invoice_status_counts: dict[str, int] = field(default_factory=dict)
    payment_status_counts: dict[str, int] = field(default_factory=dict)
    overdue_invoices: int = 0
    exception_count: int = 0

    @property
    def is_complete(self) -> bool:
        return self.quarantined_invoice_rows == 0 and self.quarantined_payment_rows == 0

    @property
    def net_difference_cents(self) -> int:
        """I - P: eligible invoices minus eligible payments."""
        return self.eligible_invoice_cents - self.eligible_payment_cents
