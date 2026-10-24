from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class AccountType(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class Direction(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"

    def opposite(self) -> "Direction":
        return Direction.CREDIT if self is Direction.DEBIT else Direction.DEBIT


class TransactionKind(StrEnum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    TRANSFER = "transfer"
    HOLD_CAPTURE = "hold_capture"
    REVERSAL = "reversal"
    ADJUSTMENT = "adjustment"


class HoldStatus(StrEnum):
    ACTIVE = "active"
    CAPTURED = "captured"
    RELEASED = "released"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class Account:
    id: UUID
    code: str
    name: str
    type: AccountType
    currency: str
    allow_negative: bool
    owner_ref: str | None
    is_closed: bool
    created_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Account":
        return cls(
            id=row["id"],
            code=row["code"],
            name=row["name"],
            type=AccountType(row["type"]),
            currency=row["currency"],
            allow_negative=row["allow_negative"],
            owner_ref=row["owner_ref"],
            is_closed=row["is_closed"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True, slots=True)
class EntryDraft:
    """One leg of a transaction before it is posted. The currency comes from the account."""

    account_id: UUID
    direction: Direction
    amount_minor: int


@dataclass(frozen=True, slots=True)
class Entry:
    id: int
    account_id: UUID
    currency: str
    direction: Direction
    amount_minor: int
    account_seq: int
    balance_after_minor: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Entry":
        return cls(
            id=row["id"],
            account_id=row["account_id"],
            currency=row["currency"],
            direction=Direction(row["direction"]),
            amount_minor=row["amount_minor"],
            account_seq=row["account_seq"],
            balance_after_minor=row["balance_after_minor"],
        )


@dataclass(frozen=True, slots=True)
class Transaction:
    id: UUID
    kind: TransactionKind
    description: str
    idempotency_key: str | None
    reverses_transaction_id: UUID | None
    metadata: dict[str, Any]
    created_at: datetime
    entries: tuple[Entry, ...]
    # True when this is the stored result of an earlier request with the same idempotency key.
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class Balance:
    account_id: UUID
    balance_minor: int
    held_minor: int

    @property
    def available_minor(self) -> int:
        return self.balance_minor - self.held_minor


@dataclass(frozen=True, slots=True)
class StatementLine:
    account_seq: int
    transaction_id: UUID
    kind: TransactionKind
    description: str
    direction: Direction
    amount_minor: int
    # Positive when the entry raises the account's natural balance, negative when it lowers it.
    signed_amount_minor: int
    balance_after_minor: int
    currency: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TrialBalanceRow:
    account_id: UUID
    code: str
    name: str
    type: AccountType
    currency: str
    debits_minor: int
    credits_minor: int
    balance_minor: int
    held_minor: int


@dataclass(frozen=True, slots=True)
class IntegrityIssue:
    check_name: str
    subject: str
    expected: int
    actual: int


@dataclass(frozen=True, slots=True)
class Hold:
    id: UUID
    account_id: UUID
    amount_minor: int
    status: HoldStatus
    reference: str | None
    expires_at: datetime | None
    captured_transaction_id: UUID | None
    created_at: datetime
    resolved_at: datetime | None
    replayed: bool = False

    @classmethod
    def from_row(cls, row: dict[str, Any], *, replayed: bool = False) -> "Hold":
        return cls(
            id=row["id"],
            account_id=row["account_id"],
            amount_minor=row["amount_minor"],
            status=HoldStatus(row["status"]),
            reference=row["reference"],
            expires_at=row["expires_at"],
            captured_transaction_id=row["captured_transaction_id"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
            replayed=replayed,
        )
