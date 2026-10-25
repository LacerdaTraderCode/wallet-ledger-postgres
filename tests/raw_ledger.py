"""Helpers for tests that talk to the schema directly, bypassing the Python services."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import psycopg
import pytest


class RawLedger:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def account(
        self, code: str, kind: str, currency: str = "USD", *, allow_negative: bool = False
    ) -> UUID:
        return self.conn.execute(
            "INSERT INTO accounts (code, name, type, currency, allow_negative) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (code, code, kind, currency, allow_negative),
        ).fetchone()["id"]

    def transaction(self, kind: str = "transfer") -> UUID:
        return self.conn.execute(
            "INSERT INTO transactions (kind, description) VALUES (%s, 'test') RETURNING id",
            (kind,),
        ).fetchone()["id"]

    def entry(
        self,
        transaction_id: UUID,
        account_id: UUID,
        direction: str,
        amount: int,
        currency: str = "USD",
    ) -> None:
        self.conn.execute(
            "INSERT INTO entries (transaction_id, account_id, currency, direction, amount_minor) "
            "VALUES (%s, %s, %s, %s, %s)",
            (transaction_id, account_id, currency, direction, amount),
        )

    def post(
        self, kind: str, debit: UUID, credit: UUID, amount: int, currency: str = "USD"
    ) -> UUID:
        transaction_id = self.transaction(kind)
        self.entry(transaction_id, debit, "debit", amount, currency)
        self.entry(transaction_id, credit, "credit", amount, currency)
        return transaction_id

    def balance(self, account_id: UUID) -> int:
        return self.conn.execute(
            "SELECT balance_minor FROM account_balances WHERE account_id = %s", (account_id,)
        ).fetchone()["balance_minor"]

    def held(self, account_id: UUID) -> int:
        return self.conn.execute(
            "SELECT held_minor FROM account_balances WHERE account_id = %s", (account_id,)
        ).fetchone()["held_minor"]


@contextmanager
def rejected_with(sqlstate: str) -> Iterator[None]:
    """Expect the database to refuse the statements inside, with this SQLSTATE.

    Deferred checks fire at commit, so the block should end by committing.
    """
    with pytest.raises(psycopg.Error) as excinfo:
        yield
    assert excinfo.value.sqlstate == sqlstate, str(excinfo.value)
