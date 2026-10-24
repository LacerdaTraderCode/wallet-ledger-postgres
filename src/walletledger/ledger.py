import dataclasses
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from walletledger import errors
from walletledger.database import IdempotencyRace, translated_errors, with_retries
from walletledger.models import (
    Account,
    AccountType,
    Balance,
    Direction,
    Entry,
    EntryDraft,
    IntegrityIssue,
    StatementLine,
    Transaction,
    TransactionKind,
    TrialBalanceRow,
)
from walletledger.money import MAX_AMOUNT_MINOR, normalize_currency

ACCOUNT_COLUMNS = "id, code, name, type, currency, allow_negative, owner_ref, is_closed, created_at"
ENTRY_COLUMNS = (
    "id, account_id, currency, direction, amount_minor, account_seq, balance_after_minor"
)

MAX_STATEMENT_PAGE = 500


def _dump_json(value: Any) -> str:
    # Metadata often carries ids; store them as text instead of failing on UUID objects.
    return json.dumps(value, default=str)


def fingerprint_of(payload: Mapping[str, Any]) -> str:
    """Stable hash of a request, stored next to its idempotency key to catch key reuse."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LedgerService:
    """The double-entry engine: accounts, balanced transactions and the reports over them.

    Every method takes an optional `conn`. Without one, the method runs in its own
    database transaction (retried if PostgreSQL picks it as a deadlock victim). With
    one, it joins the caller's transaction, which is how a use case such as capturing
    a hold changes several tables atomically.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def execute[T](self, operation: Callable[[Connection], T], conn: Connection | None = None) -> T:
        if conn is not None:
            with translated_errors():
                return operation(conn)

        def attempt() -> T:
            with translated_errors(), self._pool.connection() as owned:
                return operation(owned)

        return with_retries(attempt)

    # --- accounts -------------------------------------------------------------------------

    def open_account(
        self,
        *,
        code: str,
        name: str,
        account_type: AccountType,
        currency: str,
        allow_negative: bool = False,
        owner_ref: str | None = None,
        conn: Connection | None = None,
    ) -> Account:
        if not code.strip() or not name.strip():
            raise errors.InvalidRequest("an account needs a code and a name")
        currency = normalize_currency(currency)

        def operation(c: Connection) -> Account:
            row = c.execute(
                f"INSERT INTO accounts (code, name, type, currency, allow_negative, owner_ref) "
                f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {ACCOUNT_COLUMNS}",
                (code, name, account_type.value, currency, allow_negative, owner_ref),
            ).fetchone()
            return Account.from_row(row)

        return self.execute(operation, conn)

    def ensure_account(
        self,
        *,
        code: str,
        name: str,
        account_type: AccountType,
        currency: str,
        allow_negative: bool = False,
        conn: Connection | None = None,
    ) -> Account:
        """Create the account if missing and return it either way; safe to call concurrently."""
        currency = normalize_currency(currency)

        def operation(c: Connection) -> Account:
            row = c.execute(
                f"INSERT INTO accounts (code, name, type, currency, allow_negative) "
                f"VALUES (%s, %s, %s, %s, %s) ON CONFLICT (code) DO NOTHING "
                f"RETURNING {ACCOUNT_COLUMNS}",
                (code, name, account_type.value, currency, allow_negative),
            ).fetchone()
            if row is None:
                row = c.execute(
                    f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE code = %s", (code,)
                ).fetchone()
            account = Account.from_row(row)
            wanted = (account_type, currency, allow_negative)
            if (account.type, account.currency, account.allow_negative) != wanted:
                raise errors.InvalidRequest(
                    f"account {code!r} already exists with different settings"
                )
            return account

        return self.execute(operation, conn)

    def get_account(self, account_id: UUID, conn: Connection | None = None) -> Account:
        def operation(c: Connection) -> Account:
            row = c.execute(
                f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE id = %s", (account_id,)
            ).fetchone()
            if row is None:
                raise errors.AccountNotFound(f"account {account_id} does not exist")
            return Account.from_row(row)

        return self.execute(operation, conn)

    def find_account_by_code(self, code: str, conn: Connection | None = None) -> Account | None:
        def operation(c: Connection) -> Account | None:
            row = c.execute(
                f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE code = %s", (code,)
            ).fetchone()
            return None if row is None else Account.from_row(row)

        return self.execute(operation, conn)

    def close_account(self, account_id: UUID, conn: Connection | None = None) -> Account:
        def operation(c: Connection) -> Account:
            row = c.execute(
                f"UPDATE accounts SET is_closed = true WHERE id = %s RETURNING {ACCOUNT_COLUMNS}",
                (account_id,),
            ).fetchone()
            if row is None:
                raise errors.AccountNotFound(f"account {account_id} does not exist")
            return Account.from_row(row)

        return self.execute(operation, conn)

    # --- posting --------------------------------------------------------------------------

    def post(
        self,
        *,
        kind: TransactionKind,
        description: str,
        entries: Sequence[EntryDraft],
        idempotency_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        transaction_id: UUID | None = None,
        conn: Connection | None = None,
    ) -> Transaction:
        """Post a balanced transaction.

        With an idempotency key, repeating the same request returns the stored
        transaction (`replayed=True`) and a different request under the same key
        is refused.
        """
        return self.execute(
            lambda c: self._post(
                c,
                kind=kind,
                description=description,
                entries=entries,
                idempotency_key=idempotency_key,
                metadata=metadata or {},
                transaction_id=transaction_id,
                reverses=None,
            ),
            conn,
        )

    def reverse(
        self,
        transaction_id: UUID,
        *,
        reason: str,
        idempotency_key: str | None = None,
        conn: Connection | None = None,
    ) -> Transaction:
        """Undo a transaction by posting its mirror image. The original is never touched."""
        reason = reason.strip()
        if not reason:
            raise errors.InvalidRequest("a reversal needs a reason")

        def operation(c: Connection) -> Transaction:
            original = self._load_transaction(c, transaction_id)
            mirrored = [
                EntryDraft(entry.account_id, entry.direction.opposite(), entry.amount_minor)
                for entry in original.entries
            ]
            return self._post(
                c,
                kind=TransactionKind.REVERSAL,
                description=f"Reversal of {transaction_id}: {reason}",
                entries=mirrored,
                idempotency_key=idempotency_key,
                metadata={"reason": reason},
                transaction_id=None,
                reverses=transaction_id,
            )

        return self.execute(operation, conn)

    def lock_balances(self, conn: Connection, account_ids: Iterable[UUID]) -> None:
        """Take the balance locks of several accounts, lowest id first, in one statement.

        Every operation that touches more than one balance must acquire them in this same
        order. Two operations that disagree on the order can end up each holding the lock
        the other one wants, and PostgreSQL has to kill one of them after a second of waiting.
        """
        conn.execute(
            "SELECT 1 FROM account_balances WHERE account_id = ANY(%s) "
            "ORDER BY account_id FOR UPDATE",
            (sorted(set(account_ids)),),
        ).fetchall()

    def idempotency_key_exists(self, conn: Connection, key: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM transactions WHERE idempotency_key = %s", (key,)
        ).fetchone()
        return row is not None

    def _post(
        self,
        conn: Connection,
        *,
        kind: TransactionKind,
        description: str,
        entries: Sequence[EntryDraft],
        idempotency_key: str | None,
        metadata: Mapping[str, Any],
        transaction_id: UUID | None,
        reverses: UUID | None,
    ) -> Transaction:
        description = description.strip()
        if not description:
            raise errors.InvalidRequest("a transaction needs a description")
        if idempotency_key is not None and not 1 <= len(idempotency_key) <= 200:
            raise errors.InvalidRequest("an idempotency key must have between 1 and 200 characters")

        drafts = list(entries)
        _validate_drafts(drafts)
        currencies = self._currencies_of(conn, {draft.account_id for draft in drafts})
        _require_balanced(drafts, currencies)

        fingerprint = None
        if idempotency_key is not None:
            fingerprint = fingerprint_of(
                {
                    "kind": kind.value,
                    "description": description,
                    "entries": sorted(
                        [str(d.account_id), d.direction.value, d.amount_minor] for d in drafts
                    ),
                    "metadata": dict(metadata),
                    "reverses": reverses,
                }
            )

        inserted = conn.execute(
            "INSERT INTO transactions "
            "(id, kind, description, idempotency_key, request_fingerprint, "
            " reverses_transaction_id, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id, created_at",
            (
                transaction_id or uuid4(),
                kind.value,
                description,
                idempotency_key,
                fingerprint,
                reverses,
                Jsonb(dict(metadata), dumps=_dump_json),
            ),
        ).fetchone()
        if inserted is None:
            return self._replay(conn, idempotency_key, fingerprint)

        # Entries go in a fixed order so two transactions touching the same accounts
        # always take their balance locks in the same order and cannot deadlock each other.
        stored: list[Entry] = []
        for draft in sorted(drafts, key=lambda d: (str(d.account_id), d.direction.value)):
            row = conn.execute(
                "INSERT INTO entries "
                "(transaction_id, account_id, currency, direction, amount_minor) "
                f"VALUES (%s, %s, %s, %s, %s) RETURNING {ENTRY_COLUMNS}",
                (
                    inserted["id"],
                    draft.account_id,
                    currencies[draft.account_id],
                    draft.direction.value,
                    draft.amount_minor,
                ),
            ).fetchone()
            stored.append(Entry.from_row(row))

        # Run the deferred balance checks now, so a failure surfaces from this call
        # instead of from whenever the surrounding transaction commits.
        conn.execute("SET CONSTRAINTS entries_must_balance, transactions_need_entries IMMEDIATE")
        conn.execute("SET CONSTRAINTS entries_must_balance, transactions_need_entries DEFERRED")

        return Transaction(
            id=inserted["id"],
            kind=kind,
            description=description,
            idempotency_key=idempotency_key,
            reverses_transaction_id=reverses,
            metadata=dict(metadata),
            created_at=inserted["created_at"],
            entries=tuple(stored),
        )

    def _replay(self, conn: Connection, key: str | None, fingerprint: str | None) -> Transaction:
        row = conn.execute(
            "SELECT id, request_fingerprint FROM transactions WHERE idempotency_key = %s", (key,)
        ).fetchone()
        if row is None:
            raise IdempotencyRace()
        if row["request_fingerprint"] != fingerprint:
            raise errors.IdempotencyConflict(
                f"idempotency key {key!r} was already used with a different request"
            )
        return dataclasses.replace(self._load_transaction(conn, row["id"]), replayed=True)

    def _currencies_of(self, conn: Connection, account_ids: set[UUID]) -> dict[UUID, str]:
        rows = conn.execute(
            "SELECT id, currency FROM accounts WHERE id = ANY(%s)", (list(account_ids),)
        ).fetchall()
        found = {row["id"]: row["currency"] for row in rows}
        missing = sorted(str(account_id) for account_id in account_ids - found.keys())
        if missing:
            raise errors.AccountNotFound(f"account {missing[0]} does not exist")
        return found

    # --- reading --------------------------------------------------------------------------

    def get_transaction(self, transaction_id: UUID, conn: Connection | None = None) -> Transaction:
        return self.execute(lambda c: self._load_transaction(c, transaction_id), conn)

    def _load_transaction(self, conn: Connection, transaction_id: UUID) -> Transaction:
        row = conn.execute(
            "SELECT id, kind, description, idempotency_key, reverses_transaction_id, metadata, "
            "created_at FROM transactions WHERE id = %s",
            (transaction_id,),
        ).fetchone()
        if row is None:
            raise errors.TransactionNotFound(f"transaction {transaction_id} does not exist")
        entry_rows = conn.execute(
            f"SELECT {ENTRY_COLUMNS} FROM entries WHERE transaction_id = %s ORDER BY id",
            (transaction_id,),
        ).fetchall()
        return Transaction(
            id=row["id"],
            kind=TransactionKind(row["kind"]),
            description=row["description"],
            idempotency_key=row["idempotency_key"],
            reverses_transaction_id=row["reverses_transaction_id"],
            metadata=row["metadata"],
            created_at=row["created_at"],
            entries=tuple(Entry.from_row(entry_row) for entry_row in entry_rows),
        )

    def balance(self, account_id: UUID, conn: Connection | None = None) -> Balance:
        def operation(c: Connection) -> Balance:
            row = c.execute(
                "SELECT account_id, balance_minor, held_minor FROM account_balances "
                "WHERE account_id = %s",
                (account_id,),
            ).fetchone()
            if row is None:
                raise errors.AccountNotFound(f"account {account_id} does not exist")
            return Balance(row["account_id"], row["balance_minor"], row["held_minor"])

        return self.execute(operation, conn)

    def statement(
        self,
        account_id: UUID,
        *,
        limit: int = 50,
        before_seq: int | None = None,
        conn: Connection | None = None,
    ) -> list[StatementLine]:
        """Entries of one account, newest first, paged by the per-account sequence number."""
        if not 1 <= limit <= MAX_STATEMENT_PAGE:
            raise errors.InvalidRequest(f"limit must be between 1 and {MAX_STATEMENT_PAGE}")

        def operation(c: Connection) -> list[StatementLine]:
            self._require_account(c, account_id)
            rows = c.execute(
                """
                SELECT e.account_seq, e.transaction_id, t.kind, t.description, e.direction,
                       e.amount_minor,
                       CASE WHEN e.direction = normal_direction(a.type)
                            THEN e.amount_minor ELSE -e.amount_minor END AS signed_amount_minor,
                       e.balance_after_minor, e.currency, e.created_at
                  FROM entries e
                  JOIN transactions t ON t.id = e.transaction_id
                  JOIN accounts a ON a.id = e.account_id
                 WHERE e.account_id = %s AND (%s::bigint IS NULL OR e.account_seq < %s)
                 ORDER BY e.account_seq DESC
                 LIMIT %s
                """,
                (account_id, before_seq, before_seq, limit),
            ).fetchall()
            return [
                StatementLine(
                    account_seq=row["account_seq"],
                    transaction_id=row["transaction_id"],
                    kind=TransactionKind(row["kind"]),
                    description=row["description"],
                    direction=Direction(row["direction"]),
                    amount_minor=row["amount_minor"],
                    signed_amount_minor=row["signed_amount_minor"],
                    balance_after_minor=row["balance_after_minor"],
                    currency=row["currency"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

        return self.execute(operation, conn)

    def _require_account(self, conn: Connection, account_id: UUID) -> None:
        if conn.execute("SELECT 1 FROM accounts WHERE id = %s", (account_id,)).fetchone() is None:
            raise errors.AccountNotFound(f"account {account_id} does not exist")

    def trial_balance(self, conn: Connection | None = None) -> list[TrialBalanceRow]:
        def operation(c: Connection) -> list[TrialBalanceRow]:
            rows = c.execute(
                "SELECT account_id, code, name, type, currency, debits_minor, credits_minor, "
                "balance_minor, held_minor FROM trial_balance ORDER BY currency, code"
            ).fetchall()
            return [TrialBalanceRow(**{**row, "type": AccountType(row["type"])}) for row in rows]

        return self.execute(operation, conn)

    def integrity_report(self, conn: Connection | None = None) -> list[IntegrityIssue]:
        """Recompute what the triggers maintain; an empty list means the ledger is consistent."""

        def operation(c: Connection) -> list[IntegrityIssue]:
            rows = c.execute(
                "SELECT check_name, subject, expected, actual FROM ledger_integrity_report() "
                "ORDER BY check_name, subject"
            ).fetchall()
            return [IntegrityIssue(**row) for row in rows]

        return self.execute(operation, conn)


def _validate_drafts(drafts: Sequence[EntryDraft]) -> None:
    if len(drafts) < 2:
        raise errors.InvalidRequest("a transaction needs at least two entries")
    for draft in drafts:
        if not isinstance(draft.direction, Direction):
            raise errors.InvalidRequest("each entry needs a direction")
        if not isinstance(draft.amount_minor, int) or isinstance(draft.amount_minor, bool):
            raise errors.InvalidRequest("entry amounts must be integers in minor units")
        if not 0 < draft.amount_minor <= MAX_AMOUNT_MINOR:
            raise errors.InvalidRequest(
                "entry amounts must be positive and within the supported range"
            )


def _require_balanced(drafts: Sequence[EntryDraft], currencies: Mapping[UUID, str]) -> None:
    difference: dict[str, int] = {}
    for draft in drafts:
        signed = draft.amount_minor if draft.direction is Direction.DEBIT else -draft.amount_minor
        currency = currencies[draft.account_id]
        difference[currency] = difference.get(currency, 0) + signed
    for currency, amount in sorted(difference.items()):
        if amount != 0:
            raise errors.UnbalancedTransaction(
                f"debits and credits differ by {amount} minor units in {currency}"
            )
