import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import resources

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from walletledger import errors

_MIGRATION_LOCK_ID = 727_001


class IdempotencyRace(Exception):
    """Two requests with the same idempotency key ran at once; the loser must start over."""


def create_pool(database_url: str, *, min_size: int = 1, max_size: int = 10) -> ConnectionPool:
    pool = ConnectionPool(
        database_url,
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    pool.open(wait=True)
    return pool


def migrate(database_url: str) -> list[str]:
    """Apply the SQL migrations that have not run yet, in file order, and return their names."""
    files = sorted(
        (
            entry
            for entry in resources.files("walletledger").joinpath("migrations").iterdir()
            if entry.name.endswith(".sql")
        ),
        key=lambda entry: entry.name,
    )
    applied_now: list[str] = []
    with psycopg.connect(database_url) as conn:
        # A session lock, so two processes starting together do not run the same migration.
        conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_ID,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.commit()
        already_applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for migration in files:
            if migration.name in already_applied:
                continue
            conn.execute(migration.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (migration.name,))
            conn.commit()
            applied_now.append(migration.name)
    return applied_now


_STATE_ERRORS: dict[str, type[errors.LedgerError]] = {
    "LD001": errors.InsufficientFunds,
    "LD002": errors.UnbalancedTransaction,
    "LD003": errors.AccountClosed,
    "LD008": errors.AccountNotEmpty,
    "LD009": errors.HoldNotActive,
}

_UNIQUE_CONSTRAINT_ERRORS: dict[str, type[errors.LedgerError]] = {
    "accounts_code_key": errors.DuplicateAccount,
    "transactions_reverses_transaction_id_key": errors.AlreadyReversed,
}


def translate_error(exc: psycopg.Error) -> errors.LedgerError | None:
    """Map a database error to a domain error, or None when it is not one the ledger expects."""
    message = exc.diag.message_primary or str(exc)
    if exc.sqlstate in _STATE_ERRORS:
        return _STATE_ERRORS[exc.sqlstate](message)
    if exc.sqlstate == "23505" and exc.diag.constraint_name in _UNIQUE_CONSTRAINT_ERRORS:
        return _UNIQUE_CONSTRAINT_ERRORS[exc.diag.constraint_name](message)
    return None


@contextmanager
def translated_errors() -> Iterator[None]:
    try:
        yield
    except psycopg.Error as exc:
        translated = translate_error(exc)
        if translated is None:
            raise
        raise translated from exc


_RETRYABLE = (pg_errors.DeadlockDetected, pg_errors.SerializationFailure, IdempotencyRace)


def with_retries[T](operation: Callable[[], T], *, attempts: int = 5) -> T:
    """Run `operation`, starting over when PostgreSQL picked it as a deadlock victim.

    Only used when the service owns the transaction, so a retry always begins from scratch.
    """
    for attempt in range(attempts):
        try:
            return operation()
        except _RETRYABLE:
            if attempt == attempts - 1:
                raise
            time.sleep(0.01 * 2**attempt * (0.5 + random.random()))
    raise AssertionError("unreachable")  # pragma: no cover
