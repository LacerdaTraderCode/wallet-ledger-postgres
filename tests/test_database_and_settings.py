import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors as pg_errors
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from walletledger import errors
from walletledger.api import create_app
from walletledger.database import IdempotencyRace, migrate, translate_error, with_retries
from walletledger.ledger import fingerprint_of
from walletledger.models import AccountType, Direction, EntryDraft, TransactionKind
from walletledger.settings import Settings

MIGRATIONS = ["0001_ledger_core.sql", "0002_holds.sql", "0003_reporting.sql"]


@pytest.fixture
def empty_database_url(admin_url):
    name = (
        "empty_" + threading.current_thread().name.replace("-", "_").lower() + str(time.time_ns())
    )
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(admin_url, dbname=name)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def applied_versions(url):
    with psycopg.connect(url) as conn:
        return [
            r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]


class TestMigrations:
    def test_applies_every_migration_in_order_on_an_empty_database(self, empty_database_url):
        assert migrate(empty_database_url) == MIGRATIONS
        assert applied_versions(empty_database_url) == MIGRATIONS

    def test_running_again_applies_nothing(self, empty_database_url):
        migrate(empty_database_url)

        assert migrate(empty_database_url) == []
        assert applied_versions(empty_database_url) == MIGRATIONS

    def test_only_the_missing_migrations_run(self, empty_database_url):
        migrate(empty_database_url)
        with psycopg.connect(empty_database_url) as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version = '0003_reporting.sql'")
            conn.execute("DROP VIEW trial_balance")
            conn.execute("DROP FUNCTION ledger_integrity_report()")

        assert migrate(empty_database_url) == ["0003_reporting.sql"]

    def test_processes_starting_together_do_not_apply_a_migration_twice(self, empty_database_url):
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: migrate(empty_database_url), range(6)))

        assert sorted(name for applied in results for name in applied) == MIGRATIONS
        assert applied_versions(empty_database_url) == MIGRATIONS


class TestRetries:
    def test_returns_the_result_of_an_operation_that_works(self):
        assert with_retries(lambda: "done") == "done"

    def test_starts_over_after_a_deadlock_and_returns_the_next_result(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise pg_errors.DeadlockDetected("deadlock detected")
            return "finally"

        assert with_retries(flaky) == "finally"
        assert len(attempts) == 3

    @pytest.mark.parametrize(
        "failure",
        [pg_errors.SerializationFailure("could not serialize"), IdempotencyRace()],
    )
    def test_other_retryable_failures_are_retried_too(self, failure):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise failure
            return "ok"

        assert with_retries(flaky) == "ok"

    def test_gives_up_and_raises_after_the_last_attempt(self):
        attempts = []

        def always_deadlocked():
            attempts.append(1)
            raise pg_errors.DeadlockDetected("deadlock detected")

        with pytest.raises(pg_errors.DeadlockDetected):
            with_retries(always_deadlocked, attempts=3)
        assert len(attempts) == 3

    def test_does_not_retry_errors_that_are_not_transient(self):
        attempts = []

        def broken():
            attempts.append(1)
            raise ValueError("a bug, not a deadlock")

        with pytest.raises(ValueError):
            with_retries(broken)
        assert len(attempts) == 1


class TestErrorTranslation:
    def test_an_error_the_ledger_does_not_expect_is_not_disguised(self, ledger):
        def insert_a_bad_currency(conn):
            conn.execute(
                "INSERT INTO accounts (code, name, type, currency) "
                "VALUES ('x', 'x', 'asset', 'usd')"
            )

        with pytest.raises(pg_errors.CheckViolation):
            ledger.execute(insert_a_bad_currency)

    def test_translate_error_returns_nothing_for_unknown_errors(self):
        assert translate_error(pg_errors.CheckViolation("nope")) is None

    def test_a_failed_deferred_check_is_translated_when_it_surfaces_at_commit(self, ledger):
        # Bypass the Python pre-checks to prove the database's own refusal is still translated.
        accounts = [
            ledger.open_account(code="a", name="a", account_type=AccountType.ASSET, currency="USD"),
            ledger.open_account(
                code="b", name="b", account_type=AccountType.LIABILITY, currency="USD"
            ),
        ]

        def unbalanced_straight_to_sql(conn):
            transaction_id = conn.execute(
                "INSERT INTO transactions (kind, description) "
                "VALUES ('adjustment', 't') RETURNING id"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO entries "
                "(transaction_id, account_id, currency, direction, amount_minor) "
                "VALUES (%s, %s, 'USD', 'debit', 10), (%s, %s, 'USD', 'credit', 7)",
                (transaction_id, accounts[0].id, transaction_id, accounts[1].id),
            )

        with pytest.raises(errors.UnbalancedTransaction):
            ledger.execute(unbalanced_straight_to_sql)


class TestEntryValidation:
    def test_an_entry_direction_must_be_a_direction(self, ledger):
        first, second = (
            ledger.open_account(code=c, name=c, account_type=AccountType.ASSET, currency="USD")
            for c in ("a", "b")
        )

        with pytest.raises(errors.InvalidRequest, match="direction"):
            ledger.post(
                kind=TransactionKind.ADJUSTMENT,
                description="Stringly typed",
                entries=[
                    EntryDraft(first.id, "debit", 5),
                    EntryDraft(second.id, Direction.CREDIT, 5),
                ],
            )


class TestHoldIdempotencyRace:
    def test_a_request_that_loses_the_race_for_a_key_returns_the_winners_hold(
        self, wallets, make_wallet, database_url
    ):
        wallet = make_wallet(1000)
        key = "raced-key"
        fingerprint = fingerprint_of(
            {"wallet_id": wallet.id, "amount_minor": 300, "reference": None, "expires_at": None}
        )

        with psycopg.connect(database_url, row_factory=dict_row) as winner:
            winner_hold = winner.execute(
                "INSERT INTO holds "
                "(account_id, amount_minor, idempotency_key, request_fingerprint) "
                "VALUES (%s, 300, %s, %s) RETURNING id",
                (wallet.id, key, fingerprint),
            ).fetchone()["id"]

            outcome = {}

            def loser():
                outcome["hold"] = wallets.place_hold(wallet.id, 300, idempotency_key=key)

            thread = threading.Thread(target=loser)
            thread.start()
            self.wait_until_something_is_blocked(winner)
            winner.commit()
            thread.join(timeout=10)

        assert not thread.is_alive()
        assert outcome["hold"].id == winner_hold
        assert outcome["hold"].replayed is True
        assert wallets.balance(wallet.id).held_minor == 300

    @staticmethod
    def wait_until_something_is_blocked(conn):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            blocked = conn.execute(
                "SELECT count(*) AS n FROM pg_locks WHERE NOT granted"
            ).fetchone()
            if blocked["n"]:
                return
            time.sleep(0.02)
        raise AssertionError("the competing request never blocked on the unique key")


class TestHoldKeyValidation:
    @pytest.mark.parametrize("key", ["", "k" * 201])
    def test_the_hold_key_must_have_a_reasonable_length(self, wallets, make_wallet, key):
        wallet = make_wallet(100)

        with pytest.raises(errors.InvalidRequest, match="idempotency key"):
            wallets.place_hold(wallet.id, 10, idempotency_key=key)


class TestSettings:
    def test_defaults_describe_a_local_development_setup(self):
        settings = Settings.from_env({})

        assert settings.database_url.startswith("postgresql://")
        assert settings.auto_migrate is True
        assert (settings.pool_min_size, settings.pool_max_size) == (1, 10)
        assert (settings.transfer_fee_basis_points, settings.transfer_fee_minimum_minor) == (0, 0)

    def test_reads_every_value_from_the_environment(self):
        settings = Settings.from_env(
            {
                "LEDGER_DATABASE_URL": "postgresql://u:p@db:5432/ledger",
                "LEDGER_POOL_MIN_SIZE": "2",
                "LEDGER_POOL_MAX_SIZE": "30",
                "LEDGER_AUTO_MIGRATE": "false",
                "LEDGER_TRANSFER_FEE_BPS": "150",
                "LEDGER_TRANSFER_FEE_MINIMUM_MINOR": "25",
            }
        )

        assert settings.database_url == "postgresql://u:p@db:5432/ledger"
        assert (settings.pool_min_size, settings.pool_max_size) == (2, 30)
        assert settings.auto_migrate is False
        assert (settings.transfer_fee_basis_points, settings.transfer_fee_minimum_minor) == (
            150,
            25,
        )

    @pytest.mark.parametrize(("value", "expected"), [("1", True), ("YES", True), ("no", False)])
    def test_auto_migrate_understands_common_spellings(self, value, expected):
        assert Settings.from_env({"LEDGER_AUTO_MIGRATE": value}).auto_migrate is expected


class TestApiStartup:
    def test_the_app_applies_the_migrations_when_it_starts(self, empty_database_url):
        settings = Settings(database_url=empty_database_url, auto_migrate=True, pool_max_size=2)

        with TestClient(create_app(settings)) as client:
            assert client.get("/health").json() == {"status": "ok"}
            assert (
                client.post("/wallets", json={"owner_ref": "alice", "currency": "USD"}).status_code
                == 201
            )

        assert applied_versions(empty_database_url) == MIGRATIONS
