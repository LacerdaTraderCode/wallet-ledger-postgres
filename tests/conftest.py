import itertools
import os
import tempfile
import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from walletledger.database import create_pool, migrate
from walletledger.ledger import LedgerService
from walletledger.models import Account
from walletledger.wallets import WalletService

TEMPLATE_DATABASE = "walletledger_template"


@pytest.fixture(scope="session")
def admin_url() -> Iterator[str]:
    """Connection URL of a PostgreSQL superuser.

    CI points LEDGER_TEST_ADMIN_URL at a service container. Anywhere else the tests
    start a throwaway PostgreSQL through the `pgserver` dev dependency.
    """
    external = os.environ.get("LEDGER_TEST_ADMIN_URL")
    if external:
        yield external
        return

    import pgserver

    server = pgserver.get_server(tempfile.mkdtemp(prefix="walletledger_pg_"), cleanup_mode="delete")
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture(scope="session")
def template_database(admin_url: str) -> str:
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(TEMPLATE_DATABASE)
            )
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEMPLATE_DATABASE)))
    migrate(make_conninfo(admin_url, dbname=TEMPLATE_DATABASE))
    return TEMPLATE_DATABASE


@pytest.fixture
def database_url(admin_url: str, template_database: str) -> Iterator[str]:
    """A fresh, already migrated database for one test, cloned from the template."""
    name = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(template_database)
            )
        )
    try:
        yield make_conninfo(admin_url, dbname=name)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def pool(database_url: str) -> Iterator[ConnectionPool]:
    connection_pool = create_pool(database_url, min_size=1, max_size=24)
    try:
        yield connection_pool
    finally:
        connection_pool.close()


@pytest.fixture
def ledger(pool: ConnectionPool) -> LedgerService:
    return LedgerService(pool)


@pytest.fixture
def wallets(ledger: LedgerService) -> WalletService:
    return WalletService(ledger)


@pytest.fixture
def connection(database_url: str) -> Iterator[psycopg.Connection]:
    """A superuser connection for tests that poke the database directly."""
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        yield conn


@pytest.fixture
def make_wallet(wallets: WalletService) -> Callable[..., Account]:
    """Open a wallet, optionally funded with a deposit."""
    counter = itertools.count(1)

    def make(balance_minor: int = 0, currency: str = "USD") -> Account:
        wallet = wallets.open_wallet(f"user{next(counter)}", currency)
        if balance_minor:
            wallets.deposit(wallet.id, balance_minor, idempotency_key=f"fund-{wallet.id}")
        return wallet

    return make
