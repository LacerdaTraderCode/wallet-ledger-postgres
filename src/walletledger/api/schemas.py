from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class MoneyOut(BaseModel):
    amount: str
    amount_minor: int
    currency: str


class WalletCreate(BaseModel):
    owner_ref: str = Field(min_length=1, max_length=64)
    currency: str = Field(min_length=3, max_length=3)


class WalletOut(BaseModel):
    id: UUID
    owner_ref: str
    currency: str
    is_closed: bool
    balance: MoneyOut
    held: MoneyOut
    available: MoneyOut


class AmountIn(BaseModel):
    amount: str
    reference: str | None = Field(default=None, max_length=200)


class TransferIn(BaseModel):
    from_wallet_id: UUID
    to_wallet_id: UUID
    amount: str
    reference: str | None = Field(default=None, max_length=200)


class HoldIn(BaseModel):
    amount: str
    reference: str | None = Field(default=None, max_length=200)
    expires_at: datetime | None = None


class CaptureIn(BaseModel):
    destination_wallet_id: UUID
    amount: str | None = None
    reference: str | None = Field(default=None, max_length=200)


class ReversalIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class EntryOut(BaseModel):
    account_id: UUID
    direction: str
    amount: str
    amount_minor: int
    currency: str
    account_seq: int
    balance_after: str


class TransactionOut(BaseModel):
    id: UUID
    kind: str
    description: str
    created_at: datetime
    reverses_transaction_id: UUID | None
    replayed: bool
    entries: list[EntryOut]


class HoldOut(BaseModel):
    id: UUID
    wallet_id: UUID
    amount: MoneyOut
    status: str
    reference: str | None
    expires_at: datetime | None
    captured_transaction_id: UUID | None
    created_at: datetime
    resolved_at: datetime | None
    replayed: bool


class StatementLineOut(BaseModel):
    account_seq: int
    transaction_id: UUID
    kind: str
    description: str
    direction: str
    amount: str
    signed_amount: str
    balance_after: str
    currency: str
    created_at: datetime


class StatementOut(BaseModel):
    lines: list[StatementLineOut]
    next_before_seq: int | None


class TrialBalanceRowOut(BaseModel):
    account_id: UUID
    code: str
    name: str
    type: str
    currency: str
    debits: str
    credits: str
    balance: str
    held: str


class CurrencyTotalOut(BaseModel):
    currency: str
    debits: str
    credits: str
    balanced: bool


class TrialBalanceOut(BaseModel):
    rows: list[TrialBalanceRowOut]
    totals: list[CurrencyTotalOut]


class IntegrityIssueOut(BaseModel):
    check_name: str
    subject: str
    expected: int
    actual: int


class IntegrityOut(BaseModel):
    ok: bool
    issues: list[IntegrityIssueOut]
