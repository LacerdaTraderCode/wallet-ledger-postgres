"""Many threads hitting the same accounts at once.

Each test ends by asking the database to audit itself, because the point of these
tests is that no interleaving of requests can leave the ledger inconsistent.
"""

import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.rows import dict_row

from walletledger import database, errors
from walletledger.models import HoldStatus

WORKERS = 16


def run_concurrently(tasks):
    """Run every task on a thread pool and return ("ok", value) or ("error", exception) for each."""

    def guarded(task):
        try:
            return "ok", task()
        except Exception as exc:
            return "error", exc

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(guarded, task) for task in tasks]
        return [future.result() for future in futures]


def successes(outcomes):
    return [value for status, value in outcomes if status == "ok"]


def failures(outcomes):
    return [value for status, value in outcomes if status == "error"]


@pytest.fixture(autouse=True)
def system_accounts_ready(wallets):
    wallets.ensure_system_accounts("USD")


def test_concurrent_withdrawals_can_never_overdraw_a_wallet(wallets, ledger, make_wallet):
    wallet = make_wallet(1000)

    outcomes = run_concurrently(
        [lambda n=n: wallets.withdraw(wallet.id, 100, idempotency_key=f"wd-{n}") for n in range(40)]
    )

    assert len(successes(outcomes)) == 10
    assert all(isinstance(error, errors.InsufficientFunds) for error in failures(outcomes))
    assert len(failures(outcomes)) == 30
    assert wallets.balance(wallet.id).balance_minor == 0
    assert ledger.integrity_report() == []


def test_transfers_in_opposite_directions_do_not_deadlock(wallets, ledger, make_wallet):
    first, second = make_wallet(100_000), make_wallet(100_000)

    def transfer(n):
        sender, receiver = (first, second) if n % 2 == 0 else (second, first)
        return wallets.transfer(sender.id, receiver.id, 1 + n % 7, idempotency_key=f"x-{n}")

    outcomes = run_concurrently([lambda n=n: transfer(n) for n in range(120)])

    assert failures(outcomes) == []
    total = wallets.balance(first.id).balance_minor + wallets.balance(second.id).balance_minor
    assert total == 200_000
    assert ledger.integrity_report() == []


def test_the_same_idempotency_key_posts_exactly_once_under_concurrency(
    wallets, ledger, make_wallet
):
    wallet = make_wallet()

    outcomes = run_concurrently(
        [lambda: wallets.deposit(wallet.id, 500, idempotency_key="only-once") for _ in range(20)]
    )

    assert failures(outcomes) == []
    transactions = successes(outcomes)
    assert len({transaction.id for transaction in transactions}) == 1
    assert sum(1 for transaction in transactions if not transaction.replayed) == 1
    assert wallets.balance(wallet.id).balance_minor == 500
    assert ledger.integrity_report() == []


def test_concurrent_holds_cannot_reserve_more_than_the_balance(wallets, ledger, make_wallet):
    wallet = make_wallet(1000)

    outcomes = run_concurrently(
        [
            lambda n=n: wallets.place_hold(wallet.id, 100, idempotency_key=f"h-{n}")
            for n in range(30)
        ]
    )

    assert len(successes(outcomes)) == 10
    assert all(isinstance(error, errors.InsufficientFunds) for error in failures(outcomes))
    balance = wallets.balance(wallet.id)
    assert (balance.balance_minor, balance.held_minor, balance.available_minor) == (1000, 1000, 0)
    assert ledger.integrity_report() == []


def test_the_same_hold_request_reserves_money_once_under_concurrency(wallets, ledger, make_wallet):
    wallet = make_wallet(1000)

    outcomes = run_concurrently(
        [lambda: wallets.place_hold(wallet.id, 300, idempotency_key="one-hold") for _ in range(20)]
    )

    assert failures(outcomes) == []
    holds = successes(outcomes)
    assert len({hold.id for hold in holds}) == 1
    assert wallets.balance(wallet.id).held_minor == 300
    assert ledger.integrity_report() == []


def test_the_same_capture_request_moves_money_once_under_concurrency(wallets, ledger, make_wallet):
    payer, merchant = make_wallet(1000), make_wallet()
    hold = wallets.place_hold(payer.id, 600)

    outcomes = run_concurrently(
        [
            lambda: wallets.capture_hold(
                hold.id, destination_wallet_id=merchant.id, idempotency_key="capture-once"
            )
            for _ in range(10)
        ]
    )

    assert failures(outcomes) == []
    assert len({transaction.id for transaction in successes(outcomes)}) == 1
    assert wallets.balance(merchant.id).balance_minor == 600
    assert wallets.balance(payer.id).balance_minor == 400
    assert ledger.integrity_report() == []


def test_a_hold_can_be_captured_by_only_one_of_many_different_requests(
    wallets, ledger, make_wallet
):
    payer, merchant = make_wallet(1000), make_wallet()
    hold = wallets.place_hold(payer.id, 600)

    outcomes = run_concurrently(
        [
            lambda n=n: wallets.capture_hold(
                hold.id, destination_wallet_id=merchant.id, idempotency_key=f"c-{n}"
            )
            for n in range(10)
        ]
    )

    assert len(successes(outcomes)) == 1
    assert all(isinstance(error, errors.HoldNotActive) for error in failures(outcomes))
    assert wallets.balance(merchant.id).balance_minor == 600
    assert ledger.integrity_report() == []


@pytest.mark.parametrize("round_number", range(5))
def test_releasing_and_capturing_the_same_hold_has_exactly_one_winner(
    wallets, ledger, make_wallet, round_number
):
    payer, merchant = make_wallet(1000), make_wallet()
    hold = wallets.place_hold(payer.id, 600)

    outcomes = run_concurrently(
        [
            lambda: wallets.release_hold(hold.id),
            lambda: wallets.capture_hold(
                hold.id, destination_wallet_id=merchant.id, idempotency_key="race"
            ),
        ]
    )

    assert len(successes(outcomes)) == 1
    assert all(isinstance(error, errors.HoldNotActive) for error in failures(outcomes))
    final = wallets.get_hold(hold.id)
    if final.status is HoldStatus.CAPTURED:
        assert wallets.balance(merchant.id).balance_minor == 600
    else:
        assert final.status is HoldStatus.RELEASED
        assert wallets.balance(merchant.id).balance_minor == 0
    assert wallets.balance(payer.id).held_minor == 0
    assert ledger.integrity_report() == []


def test_a_burst_of_mixed_activity_leaves_the_ledger_consistent(wallets, ledger, make_wallet):
    """Seeded random deposits, withdrawals, transfers and holds across a handful of wallets."""
    pool_of_wallets = [make_wallet(5_000) for _ in range(6)]
    expected_failures = (errors.InsufficientFunds, errors.HoldNotActive)

    def activity(n):
        rng = random.Random(n)
        wallet = rng.choice(pool_of_wallets)
        other = rng.choice([w for w in pool_of_wallets if w.id != wallet.id])
        amount = rng.randint(1, 900)
        match rng.choice(["deposit", "withdraw", "transfer", "capture", "release", "hold"]):
            case "deposit":
                return wallets.deposit(wallet.id, amount, idempotency_key=f"mix-{n}")
            case "withdraw":
                return wallets.withdraw(wallet.id, amount, idempotency_key=f"mix-{n}")
            case "transfer":
                return wallets.transfer(wallet.id, other.id, amount, idempotency_key=f"mix-{n}")
            case "capture":
                hold = wallets.place_hold(wallet.id, amount)
                return wallets.capture_hold(
                    hold.id, destination_wallet_id=other.id, idempotency_key=f"mix-{n}"
                )
            case "release":
                return wallets.release_hold(wallets.place_hold(wallet.id, amount).id)
            case _:
                return wallets.place_hold(wallet.id, amount)

    outcomes = run_concurrently([lambda n=n: activity(n) for n in range(300)])

    unexpected = [error for error in failures(outcomes) if not isinstance(error, expected_failures)]
    assert unexpected == []
    assert Counter(status for status, _ in outcomes)["ok"] > 150

    assert ledger.integrity_report() == []
    wallet_total = 0
    for wallet in pool_of_wallets:
        balance = wallets.balance(wallet.id)
        assert balance.available_minor >= 0
        wallet_total += balance.balance_minor
    system = wallets.ensure_system_accounts("USD")
    assert ledger.balance(system.cash.id).balance_minor == wallet_total


@pytest.fixture
def retries(monkeypatch):
    """Counts how often an operation had to start over after losing a deadlock."""
    counter = SimpleNamespace(count=0)

    def counting_sleep(_seconds):
        counter.count += 1

    monkeypatch.setattr(database, "time", SimpleNamespace(sleep=counting_sleep))
    return counter


def test_captures_and_transfers_in_opposite_directions_never_deadlock(
    wallets, ledger, make_wallet, retries
):
    """The ordering of balance locks is the same for every operation, so no cycle can form.

    The wallet with the higher id is the payer: a capture that locked it first and the
    other wallet second would cross paths with transfers going the other way.
    """
    first, second = make_wallet(1_000_000), make_wallet(1_000_000)
    low, high = sorted([first, second], key=lambda wallet: wallet.id)

    def capture_from_high(n):
        hold = wallets.place_hold(high.id, 1)
        return wallets.capture_hold(
            hold.id, destination_wallet_id=low.id, idempotency_key=f"cap-{n}"
        )

    def transfer_from_low(n):
        return wallets.transfer(low.id, high.id, 1, idempotency_key=f"tr-{n}")

    tasks = []
    for n in range(80):
        tasks.append(lambda n=n: capture_from_high(n))
        tasks.append(lambda n=n: transfer_from_low(n))
    outcomes = run_concurrently(tasks)

    assert failures(outcomes) == []
    assert retries.count == 0
    assert ledger.integrity_report() == []


class TestLockOrderIsAscending:
    """Deterministic versions of the deadlock scenarios.

    A side connection holds the lock of the wallet with the lower id, as a transfer
    in the middle of its work would. The operation under test starts and gets stuck
    waiting for it. The side connection then asks for the lock of the higher-id
    wallet. If the operation had already taken that one, both would be waiting for
    each other and PostgreSQL would have to kill one of them.
    """

    @staticmethod
    def wait_until_blocked(conn):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if conn.execute("SELECT count(*) AS n FROM pg_locks WHERE NOT granted").fetchone()["n"]:
                return
            time.sleep(0.02)
        raise AssertionError("the operation under test never waited for the lock")

    @staticmethod
    def lock_balance(conn, wallet):
        conn.execute(
            "SELECT 1 FROM account_balances WHERE account_id = %s FOR UPDATE", (wallet.id,)
        ).fetchall()

    def run_against_side_connection(self, database_url, low, high, operation):
        with psycopg.connect(database_url, row_factory=dict_row) as side:
            self.lock_balance(side, low)
            result = {}

            def target():
                result["value"] = operation()

            worker = threading.Thread(target=target)
            worker.start()
            self.wait_until_blocked(side)

            self.lock_balance(side, high)  # raises DeadlockDetected if the order was wrong
            side.commit()
            worker.join(timeout=10)
        assert not worker.is_alive()
        return result["value"]

    def test_capturing_a_hold_waits_for_the_lower_wallet_before_touching_the_higher_one(
        self, wallets, ledger, make_wallet, database_url, retries
    ):
        first, second = make_wallet(1000), make_wallet(1000)
        low, high = sorted([first, second], key=lambda wallet: wallet.id)
        hold = wallets.place_hold(high.id, 100)

        posted = self.run_against_side_connection(
            database_url,
            low,
            high,
            lambda: wallets.capture_hold(
                hold.id, destination_wallet_id=low.id, idempotency_key="ordered"
            ),
        )

        assert retries.count == 0
        assert posted.replayed is False
        assert wallets.balance(low.id).balance_minor == 1100
        assert ledger.integrity_report() == []

    def test_expiring_holds_takes_balance_locks_from_the_lowest_id_up(
        self, wallets, ledger, make_wallet, database_url, retries
    ):
        """Passing requires expiry to reach the lowest id first.

        The old single UPDATE visited holds in the order of a hash of their ids, so with 12
        wallets it started somewhere else about eleven times out of twelve, and the side
        connection's request for the others then closed the cycle.
        """
        every_wallet = [make_wallet(1000) for _ in range(12)]
        past = datetime.now(UTC) - timedelta(minutes=1)
        for wallet in every_wallet:
            wallets.place_hold(wallet.id, 100, expires_at=past)
        low, *others = sorted(every_wallet, key=lambda wallet: wallet.id)

        with psycopg.connect(database_url, row_factory=dict_row) as side:
            self.lock_balance(side, low)
            result = {}
            worker = threading.Thread(
                target=lambda: result.update(expired=wallets.expire_due_holds())
            )
            worker.start()
            self.wait_until_blocked(side)

            for wallet in others:  # raises DeadlockDetected if expiry already holds any of them
                self.lock_balance(side, wallet)
            side.commit()
            worker.join(timeout=10)

        assert not worker.is_alive()
        assert retries.count == 0
        assert result["expired"] == 12
        assert all(wallets.balance(wallet.id).held_minor == 0 for wallet in every_wallet)
        assert ledger.integrity_report() == []
