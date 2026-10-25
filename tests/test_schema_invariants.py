"""The rules that protect money are enforced by the database itself.

These tests go around the Python services and talk SQL, to show that nothing
the application does (or forgets to do) can break them.
"""

from types import SimpleNamespace

import pytest
from raw_ledger import RawLedger, rejected_with


@pytest.fixture
def raw(connection):
    return RawLedger(connection)


@pytest.fixture
def basics(raw, connection):
    accounts = SimpleNamespace(
        cash=raw.account("cash", "asset"), wallet=raw.account("wallet", "liability")
    )
    connection.commit()
    return accounts


class TestBalancedTransactions:
    def test_a_balanced_transaction_updates_both_balances(self, raw, connection, basics):
        raw.post("deposit", basics.cash, basics.wallet, 1000)
        connection.commit()

        assert raw.balance(basics.cash) == 1000
        assert raw.balance(basics.wallet) == 1000

    def test_an_unbalanced_transaction_is_rejected_at_commit(self, raw, connection, basics):
        transaction_id = raw.transaction()
        raw.entry(transaction_id, basics.cash, "debit", 500)
        raw.entry(transaction_id, basics.wallet, "credit", 400)

        with rejected_with("LD002"):
            connection.commit()

    def test_a_transaction_with_a_single_entry_is_rejected(self, raw, connection, basics):
        transaction_id = raw.transaction()
        raw.entry(transaction_id, basics.cash, "debit", 100)

        with rejected_with("LD005"):
            connection.commit()

    def test_a_transaction_without_entries_is_rejected(self, raw, connection):
        raw.transaction()

        with rejected_with("LD005"):
            connection.commit()

    def test_each_currency_must_balance_on_its_own(self, raw, connection, basics):
        euro = raw.account("euro", "asset", "EUR")
        transaction_id = raw.transaction()
        raw.entry(transaction_id, basics.cash, "debit", 100)
        raw.entry(transaction_id, basics.wallet, "credit", 100)
        raw.entry(transaction_id, euro, "debit", 50, "EUR")

        with rejected_with("LD002"):
            connection.commit()

    def test_a_multi_currency_transaction_balanced_per_currency_is_accepted(
        self, raw, connection, basics
    ):
        usd_clearing = raw.account("fx-usd", "asset", "USD", allow_negative=True)
        eur_clearing = raw.account("fx-eur", "asset", "EUR", allow_negative=True)
        eur_wallet = raw.account("wallet-eur", "liability", "EUR")
        raw.post("deposit", basics.cash, basics.wallet, 500)

        transaction_id = raw.transaction("adjustment")
        raw.entry(transaction_id, basics.wallet, "debit", 100)
        raw.entry(transaction_id, usd_clearing, "credit", 100)
        raw.entry(transaction_id, eur_clearing, "debit", 90, "EUR")
        raw.entry(transaction_id, eur_wallet, "credit", 90, "EUR")
        connection.commit()

        assert raw.balance(basics.wallet) == 400
        assert raw.balance(eur_wallet) == 90

    def test_an_entry_must_be_in_the_currency_of_its_account(self, raw, connection, basics):
        transaction_id = raw.transaction()

        with rejected_with("23503"):
            raw.entry(transaction_id, basics.cash, "debit", 100, "EUR")

    @pytest.mark.parametrize("amount", [0, -5])
    def test_amounts_must_be_positive(self, raw, basics, amount):
        transaction_id = raw.transaction()

        with rejected_with("23514"):
            raw.entry(transaction_id, basics.cash, "debit", amount)


class TestOverdraft:
    def test_spending_more_than_the_balance_is_rejected(self, raw, connection, basics):
        raw.post("deposit", basics.cash, basics.wallet, 1000)
        connection.commit()

        with rejected_with("LD001"):
            raw.post("withdrawal", basics.wallet, basics.cash, 1001)

    def test_spending_exactly_the_balance_is_allowed(self, raw, connection, basics):
        raw.post("deposit", basics.cash, basics.wallet, 1000)
        raw.post("withdrawal", basics.wallet, basics.cash, 1000)
        connection.commit()

        assert raw.balance(basics.wallet) == 0
        assert raw.balance(basics.cash) == 0

    def test_an_asset_account_cannot_go_negative_either(self, raw, basics):
        with rejected_with("LD001"):
            raw.post("withdrawal", basics.wallet, basics.cash, 1)

    def test_accounts_that_allow_negative_balances_can_overdraw(self, raw, connection):
        bank = raw.account("bank", "asset", allow_negative=True)
        equity = raw.account("equity", "equity", allow_negative=True)
        raw.post("adjustment", equity, bank, 300)
        connection.commit()

        assert raw.balance(bank) == -300
        assert raw.balance(equity) == -300


class TestAppendOnly:
    @pytest.fixture
    def posted(self, raw, connection, basics):
        transaction_id = raw.post("deposit", basics.cash, basics.wallet, 1000)
        connection.commit()
        return transaction_id

    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE entries SET amount_minor = 1",
            "DELETE FROM entries",
            "UPDATE transactions SET description = 'edited'",
            "DELETE FROM transactions",
            "TRUNCATE entries",
            "TRUNCATE transactions, entries, holds",
        ],
    )
    def test_entries_and_transactions_cannot_be_changed_or_removed(
        self, connection, posted, statement
    ):
        with rejected_with("LD006"):
            connection.execute(statement)

    def test_entries_cannot_be_added_to_a_transaction_that_already_committed(
        self, raw, connection, basics, posted
    ):
        with rejected_with("LD004"):
            raw.entry(posted, basics.cash, "debit", 1)

    def test_accounts_cannot_be_deleted(self, connection, basics):
        with rejected_with("LD006"):
            connection.execute("DELETE FROM accounts")

    @pytest.mark.parametrize(
        "assignment",
        ["code = 'renamed'", "type = 'equity'", "currency = 'EUR'", "allow_negative = true"],
    )
    def test_the_identity_of_an_account_cannot_change(self, connection, basics, assignment):
        with rejected_with("LD007"):
            connection.execute(f"UPDATE accounts SET {assignment} WHERE id = %s", (basics.wallet,))

    def test_the_display_name_of_an_account_can_change(self, connection, basics):
        connection.execute(
            "UPDATE accounts SET name = 'Nicer name' WHERE id = %s", (basics.wallet,)
        )
        connection.commit()


class TestAccountLifecycle:
    def test_an_account_without_balance_can_be_closed(self, raw, connection, basics):
        connection.execute("UPDATE accounts SET is_closed = true WHERE id = %s", (basics.wallet,))
        connection.commit()

    def test_an_account_with_a_balance_cannot_be_closed(self, raw, connection, basics):
        raw.post("deposit", basics.cash, basics.wallet, 100)
        connection.commit()

        with rejected_with("LD008"):
            connection.execute(
                "UPDATE accounts SET is_closed = true WHERE id = %s", (basics.wallet,)
            )

    def test_a_closed_account_cannot_be_reopened(self, connection, basics):
        connection.execute("UPDATE accounts SET is_closed = true WHERE id = %s", (basics.wallet,))
        connection.commit()

        with rejected_with("LD003"):
            connection.execute(
                "UPDATE accounts SET is_closed = false WHERE id = %s", (basics.wallet,)
            )

    def test_nothing_can_be_posted_to_a_closed_account(self, raw, connection, basics):
        connection.execute("UPDATE accounts SET is_closed = true WHERE id = %s", (basics.wallet,))
        connection.commit()

        with rejected_with("LD003"):
            raw.post("deposit", basics.cash, basics.wallet, 100)

    def test_opening_an_account_creates_its_balance_row(self, raw, connection, basics):
        assert raw.balance(basics.wallet) == 0
        assert raw.held(basics.wallet) == 0


class TestHoldsInTheDatabase:
    @pytest.fixture
    def funded(self, raw, connection, basics):
        raw.post("deposit", basics.cash, basics.wallet, 1000)
        connection.commit()
        return basics

    def place(self, connection, account_id, amount):
        return connection.execute(
            "INSERT INTO holds (account_id, amount_minor) VALUES (%s, %s) RETURNING id",
            (account_id, amount),
        ).fetchone()["id"]

    def test_a_hold_reserves_part_of_the_balance_without_moving_it(self, raw, connection, funded):
        self.place(connection, funded.wallet, 400)
        connection.commit()

        assert raw.balance(funded.wallet) == 1000
        assert raw.held(funded.wallet) == 400

    def test_a_hold_larger_than_the_balance_is_rejected(self, connection, funded):
        with rejected_with("LD001"):
            self.place(connection, funded.wallet, 1001)

    def test_holds_together_cannot_exceed_the_balance(self, connection, funded):
        self.place(connection, funded.wallet, 600)
        with rejected_with("LD001"):
            self.place(connection, funded.wallet, 401)

    def test_held_money_cannot_be_spent(self, raw, connection, funded):
        self.place(connection, funded.wallet, 800)
        connection.commit()

        with rejected_with("LD001"):
            raw.post("withdrawal", funded.wallet, funded.cash, 201)

    def test_money_that_is_not_held_can_still_be_spent(self, raw, connection, funded):
        self.place(connection, funded.wallet, 800)
        raw.post("withdrawal", funded.wallet, funded.cash, 200)
        connection.commit()

        assert raw.balance(funded.wallet) == 800

    def test_resolving_a_hold_frees_the_reserved_amount(self, raw, connection, funded):
        hold_id = self.place(connection, funded.wallet, 800)
        connection.execute(
            "UPDATE holds SET status = 'released', resolved_at = now() WHERE id = %s", (hold_id,)
        )
        raw.post("withdrawal", funded.wallet, funded.cash, 1000)
        connection.commit()

        assert raw.held(funded.wallet) == 0
        assert raw.balance(funded.wallet) == 0

    def test_a_resolved_hold_cannot_be_resolved_again(self, connection, funded):
        hold_id = self.place(connection, funded.wallet, 100)
        connection.execute(
            "UPDATE holds SET status = 'released', resolved_at = now() WHERE id = %s", (hold_id,)
        )
        connection.commit()

        with rejected_with("LD009"):
            connection.execute(
                "UPDATE holds SET status = 'expired', resolved_at = now() WHERE id = %s", (hold_id,)
            )

    def test_the_amount_of_a_hold_cannot_change(self, connection, funded):
        hold_id = self.place(connection, funded.wallet, 100)
        connection.commit()

        with rejected_with("LD007"):
            connection.execute("UPDATE holds SET amount_minor = 1 WHERE id = %s", (hold_id,))

    def test_holds_cannot_be_deleted(self, connection, funded):
        self.place(connection, funded.wallet, 100)
        connection.commit()

        with rejected_with("LD006"):
            connection.execute("DELETE FROM holds")

    def test_a_hold_must_start_active(self, connection, funded):
        with rejected_with("LD009"):
            connection.execute(
                "INSERT INTO holds (account_id, amount_minor, status, resolved_at) "
                "VALUES (%s, 10, 'released', now())",
                (funded.wallet,),
            )

    def test_an_account_with_an_active_hold_cannot_be_closed(self, raw, connection, basics):
        raw.post("deposit", basics.cash, basics.wallet, 100)
        self.place(connection, basics.wallet, 50)
        connection.commit()

        with rejected_with("LD008"):
            connection.execute(
                "UPDATE accounts SET is_closed = true WHERE id = %s", (basics.wallet,)
            )


class TestEntrySequencing:
    def test_each_account_numbers_its_entries_and_records_the_running_balance(
        self, raw, connection, basics
    ):
        raw.post("deposit", basics.cash, basics.wallet, 1000)
        raw.post("withdrawal", basics.wallet, basics.cash, 300)
        raw.post("deposit", basics.cash, basics.wallet, 50)
        connection.commit()

        rows = connection.execute(
            "SELECT account_seq, balance_after_minor FROM entries WHERE account_id = %s "
            "ORDER BY account_seq",
            (basics.wallet,),
        ).fetchall()

        assert [(row["account_seq"], row["balance_after_minor"]) for row in rows] == [
            (1, 1000),
            (2, 700),
            (3, 750),
        ]
