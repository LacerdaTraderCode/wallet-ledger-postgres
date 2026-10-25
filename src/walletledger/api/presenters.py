from walletledger.api import schemas
from walletledger.models import (
    Account,
    Balance,
    Hold,
    IntegrityIssue,
    StatementLine,
    Transaction,
    TrialBalanceRow,
)
from walletledger.money import format_amount


def money(amount_minor: int, currency: str) -> schemas.MoneyOut:
    return schemas.MoneyOut(
        amount=format_amount(amount_minor, currency), amount_minor=amount_minor, currency=currency
    )


def wallet(account: Account, balance: Balance) -> schemas.WalletOut:
    return schemas.WalletOut(
        id=account.id,
        owner_ref=account.owner_ref or "",
        currency=account.currency,
        is_closed=account.is_closed,
        balance=money(balance.balance_minor, account.currency),
        held=money(balance.held_minor, account.currency),
        available=money(balance.available_minor, account.currency),
    )


def transaction(posted: Transaction) -> schemas.TransactionOut:
    return schemas.TransactionOut(
        id=posted.id,
        kind=posted.kind.value,
        description=posted.description,
        created_at=posted.created_at,
        reverses_transaction_id=posted.reverses_transaction_id,
        replayed=posted.replayed,
        entries=[
            schemas.EntryOut(
                account_id=entry.account_id,
                direction=entry.direction.value,
                amount=format_amount(entry.amount_minor, entry.currency),
                amount_minor=entry.amount_minor,
                currency=entry.currency,
                account_seq=entry.account_seq,
                balance_after=format_amount(entry.balance_after_minor, entry.currency),
            )
            for entry in posted.entries
        ],
    )


def hold(item: Hold, currency: str) -> schemas.HoldOut:
    return schemas.HoldOut(
        id=item.id,
        wallet_id=item.account_id,
        amount=money(item.amount_minor, currency),
        status=item.status.value,
        reference=item.reference,
        expires_at=item.expires_at,
        captured_transaction_id=item.captured_transaction_id,
        created_at=item.created_at,
        resolved_at=item.resolved_at,
        replayed=item.replayed,
    )


def statement_line(line: StatementLine) -> schemas.StatementLineOut:
    return schemas.StatementLineOut(
        account_seq=line.account_seq,
        transaction_id=line.transaction_id,
        kind=line.kind.value,
        description=line.description,
        direction=line.direction.value,
        amount=format_amount(line.amount_minor, line.currency),
        signed_amount=format_amount(line.signed_amount_minor, line.currency),
        balance_after=format_amount(line.balance_after_minor, line.currency),
        currency=line.currency,
        created_at=line.created_at,
    )


def trial_balance(rows: list[TrialBalanceRow]) -> schemas.TrialBalanceOut:
    totals: dict[str, list[int]] = {}
    for row in rows:
        pair = totals.setdefault(row.currency, [0, 0])
        pair[0] += row.debits_minor
        pair[1] += row.credits_minor

    return schemas.TrialBalanceOut(
        rows=[
            schemas.TrialBalanceRowOut(
                account_id=row.account_id,
                code=row.code,
                name=row.name,
                type=row.type.value,
                currency=row.currency,
                debits=format_amount(row.debits_minor, row.currency),
                credits=format_amount(row.credits_minor, row.currency),
                balance=format_amount(row.balance_minor, row.currency),
                held=format_amount(row.held_minor, row.currency),
            )
            for row in rows
        ],
        totals=[
            schemas.CurrencyTotalOut(
                currency=currency,
                debits=format_amount(debits, currency),
                credits=format_amount(credits, currency),
                balanced=debits == credits,
            )
            for currency, (debits, credits) in sorted(totals.items())
        ],
    )


def integrity(issues: list[IntegrityIssue]) -> schemas.IntegrityOut:
    return schemas.IntegrityOut(
        ok=not issues,
        issues=[
            schemas.IntegrityIssueOut(
                check_name=issue.check_name,
                subject=issue.subject,
                expected=issue.expected,
                actual=issue.actual,
            )
            for issue in issues
        ],
    )
