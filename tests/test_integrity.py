"""The integrity report must notice every way the stored numbers can drift from the entries.

The triggers make drift impossible through normal use, so these tests switch them off
(`session_replication_role = replica`, which needs a superuser) and corrupt the data
by hand. This is what an audit exists for: a restored backup, a manual fix, a bug.
"""

from contextlib import contextmanager

import pytest

from walletledger.models import IntegrityIssue


@pytest.fixture
def activity(wallets, make_wallet):
    sender, receiver = make_wallet(1000), make_wallet()
    wallets.transfer(sender.id, receiver.id, 300, idempotency_key="seed-1")
    wallets.place_hold(sender.id, 100)
    return sender, receiver


@pytest.fixture
def tamper(connection):
    @contextmanager
    def tampering():
        connection.execute("SET session_replication_role = replica")
        try:
            yield connection
            connection.commit()
        finally:
            connection.execute("SET session_replication_role = DEFAULT")
            connection.commit()

    return tampering


def issues_by_check(ledger):
    grouped = {}
    for issue in ledger.integrity_report():
        grouped.setdefault(issue.check_name, []).append(issue)
    return grouped


def test_a_ledger_that_was_only_used_normally_is_consistent(ledger, activity):
    assert ledger.integrity_report() == []


def test_a_tampered_balance_is_reported_with_the_expected_and_actual_values(
    ledger, activity, tamper
):
    sender, _ = activity
    with tamper() as conn:
        conn.execute(
            "UPDATE account_balances SET balance_minor = balance_minor + 5 WHERE account_id = %s",
            (sender.id,),
        )

    assert ledger.integrity_report() == [
        IntegrityIssue("stored_balance", sender.code, expected=700, actual=705)
    ]


def test_a_tampered_hold_total_is_reported(ledger, activity, tamper):
    sender, _ = activity
    with tamper() as conn:
        conn.execute(
            "UPDATE account_balances SET held_minor = 7 WHERE account_id = %s", (sender.id,)
        )

    assert ledger.integrity_report() == [
        IntegrityIssue("stored_held", sender.code, expected=100, actual=7)
    ]


def test_a_tampered_entry_counter_is_reported(ledger, activity, tamper):
    sender, _ = activity
    with tamper() as conn:
        conn.execute(
            "UPDATE account_balances SET entry_count = entry_count + 1 WHERE account_id = %s",
            (sender.id,),
        )

    assert [issue.check_name for issue in ledger.integrity_report()] == ["stored_entry_count"]


def test_a_tampered_running_balance_is_reported_on_that_entry_only(ledger, activity, tamper):
    sender, _ = activity
    with tamper() as conn:
        conn.execute(
            "UPDATE entries SET balance_after_minor = balance_after_minor + 1 "
            "WHERE account_id = %s AND account_seq = 2",
            (sender.id,),
        )

    issues = ledger.integrity_report()

    assert [issue.check_name for issue in issues] == ["running_balance"]
    assert issues[0].subject == f"{sender.id}#2"


def test_an_edited_entry_amount_breaks_several_checks_at_once(ledger, activity, tamper):
    with tamper() as conn:
        conn.execute("UPDATE entries SET amount_minor = amount_minor + 1 WHERE id = 3")

    checks = set(issues_by_check(ledger))

    assert {
        "transaction_balanced",
        "currency_balanced",
        "stored_balance",
        "running_balance",
    } <= checks


def test_a_one_sided_entry_that_skipped_the_triggers_is_reported(ledger, activity, tamper):
    sender, _ = activity
    with tamper() as conn:
        transaction_id = conn.execute(
            "SELECT id FROM transactions ORDER BY created_at LIMIT 1"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO entries (transaction_id, account_id, currency, direction, amount_minor, "
            "account_seq, balance_after_minor) VALUES (%s, %s, 'USD', 'debit', 50, 99, 0)",
            (transaction_id, sender.id),
        )

    grouped = issues_by_check(ledger)

    assert "transaction_balanced" in grouped
    assert "currency_balanced" in grouped
    assert grouped["currency_balanced"][0].actual == 50


def test_the_trial_balance_shows_the_imbalance_too(ledger, activity, tamper):
    with tamper() as conn:
        conn.execute("UPDATE entries SET amount_minor = amount_minor + 1 WHERE id = 3")

    rows = ledger.trial_balance()

    assert sum(row.debits_minor for row in rows) != sum(row.credits_minor for row in rows)
