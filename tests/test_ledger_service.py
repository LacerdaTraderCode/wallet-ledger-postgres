from types import SimpleNamespace
from uuid import uuid4

import pytest

from walletledger import errors
from walletledger.models import AccountType, Direction, EntryDraft, TransactionKind

DEBIT, CREDIT = Direction.DEBIT, Direction.CREDIT


@pytest.fixture
def accounts(ledger):
    return SimpleNamespace(
        cash=ledger.open_account(
            code="cash", name="Cash", account_type=AccountType.ASSET, currency="USD"
        ),
        wallet=ledger.open_account(
            code="wallet", name="Wallet", account_type=AccountType.LIABILITY, currency="USD"
        ),
        other=ledger.open_account(
            code="other", name="Other wallet", account_type=AccountType.LIABILITY, currency="USD"
        ),
    )


def deposit(ledger, accounts, amount, *, key=None, to=None):
    target = to or accounts.wallet
    return ledger.post(
        kind=TransactionKind.DEPOSIT,
        description="Deposit",
        entries=[
            EntryDraft(accounts.cash.id, DEBIT, amount),
            EntryDraft(target.id, CREDIT, amount),
        ],
        idempotency_key=key,
    )


def withdraw(ledger, accounts, amount, *, key=None):
    return ledger.post(
        kind=TransactionKind.WITHDRAWAL,
        description="Withdrawal",
        entries=[
            EntryDraft(accounts.wallet.id, DEBIT, amount),
            EntryDraft(accounts.cash.id, CREDIT, amount),
        ],
        idempotency_key=key,
    )


def transaction_count(ledger):
    return ledger.execute(lambda c: c.execute("SELECT count(*) AS n FROM transactions").fetchone())[
        "n"
    ]


class TestAccounts:
    def test_opens_an_account_with_the_given_settings(self, ledger):
        account = ledger.open_account(
            code="bank",
            name="Bank",
            account_type=AccountType.ASSET,
            currency="eur",
            allow_negative=True,
            owner_ref="treasury",
        )

        assert account.code == "bank"
        assert account.type is AccountType.ASSET
        assert account.currency == "EUR"
        assert account.allow_negative is True
        assert account.owner_ref == "treasury"
        assert account.is_closed is False
        assert ledger.get_account(account.id) == account

    def test_a_new_account_starts_with_a_zero_balance(self, ledger, accounts):
        balance = ledger.balance(accounts.wallet.id)

        assert (balance.balance_minor, balance.held_minor, balance.available_minor) == (0, 0, 0)

    def test_refuses_a_second_account_with_the_same_code(self, ledger, accounts):
        with pytest.raises(errors.DuplicateAccount):
            ledger.open_account(
                code="cash", name="Again", account_type=AccountType.ASSET, currency="USD"
            )

    @pytest.mark.parametrize(("code", "name"), [("", "Name"), ("  ", "Name"), ("code", "")])
    def test_requires_a_code_and_a_name(self, ledger, code, name):
        with pytest.raises(errors.InvalidRequest):
            ledger.open_account(
                code=code, name=name, account_type=AccountType.ASSET, currency="USD"
            )

    def test_refuses_an_unsupported_currency(self, ledger):
        with pytest.raises(errors.InvalidRequest, match="unsupported currency"):
            ledger.open_account(code="x", name="X", account_type=AccountType.ASSET, currency="XXX")

    def test_reports_an_unknown_account(self, ledger):
        with pytest.raises(errors.AccountNotFound):
            ledger.get_account(uuid4())
        with pytest.raises(errors.AccountNotFound):
            ledger.balance(uuid4())

    def test_finds_an_account_by_code(self, ledger, accounts):
        assert ledger.find_account_by_code("wallet") == accounts.wallet
        assert ledger.find_account_by_code("missing") is None

    def test_ensure_account_returns_the_existing_account(self, ledger, accounts):
        again = ledger.ensure_account(
            code="cash", name="Different name", account_type=AccountType.ASSET, currency="USD"
        )

        assert again.id == accounts.cash.id

    def test_ensure_account_refuses_different_settings(self, ledger, accounts):
        with pytest.raises(errors.InvalidRequest, match="different settings"):
            ledger.ensure_account(
                code="cash", name="Cash", account_type=AccountType.LIABILITY, currency="USD"
            )

    def test_closes_an_empty_account_and_closing_again_is_harmless(self, ledger, accounts):
        assert ledger.close_account(accounts.other.id).is_closed is True
        assert ledger.close_account(accounts.other.id).is_closed is True

    def test_refuses_to_close_an_account_that_still_has_money(self, ledger, accounts):
        deposit(ledger, accounts, 500)

        with pytest.raises(errors.AccountNotEmpty):
            ledger.close_account(accounts.wallet.id)

    def test_reports_closing_an_unknown_account(self, ledger):
        with pytest.raises(errors.AccountNotFound):
            ledger.close_account(uuid4())


class TestPosting:
    def test_posts_a_balanced_transaction_and_updates_the_balances(self, ledger, accounts):
        posted = deposit(ledger, accounts, 1000)

        assert posted.kind is TransactionKind.DEPOSIT
        assert posted.replayed is False
        assert ledger.balance(accounts.wallet.id).balance_minor == 1000
        assert ledger.balance(accounts.cash.id).balance_minor == 1000

    def test_returns_the_stored_entries_with_their_sequence_and_running_balance(
        self, ledger, accounts
    ):
        deposit(ledger, accounts, 1000)
        posted = withdraw(ledger, accounts, 300)

        wallet_entry = next(e for e in posted.entries if e.account_id == accounts.wallet.id)
        assert wallet_entry.account_seq == 2
        assert wallet_entry.balance_after_minor == 700
        assert wallet_entry.direction is DEBIT

    def test_what_was_posted_can_be_read_back(self, ledger, accounts):
        posted = ledger.post(
            kind=TransactionKind.ADJUSTMENT,
            description="  Correction  ",
            entries=[
                EntryDraft(accounts.cash.id, DEBIT, 40),
                EntryDraft(accounts.wallet.id, CREDIT, 40),
            ],
            metadata={"ticket": "OPS-12"},
        )

        stored = ledger.get_transaction(posted.id)

        assert stored.description == "Correction"
        assert stored.metadata == {"ticket": "OPS-12"}
        assert {(e.account_id, e.amount_minor) for e in stored.entries} == {
            (accounts.cash.id, 40),
            (accounts.wallet.id, 40),
        }

    def test_reports_an_unknown_transaction(self, ledger):
        with pytest.raises(errors.TransactionNotFound):
            ledger.get_transaction(uuid4())

    def test_an_unbalanced_transaction_is_refused_and_nothing_is_stored(self, ledger, accounts):
        with pytest.raises(errors.UnbalancedTransaction, match="differ by"):
            ledger.post(
                kind=TransactionKind.ADJUSTMENT,
                description="Broken",
                entries=[
                    EntryDraft(accounts.cash.id, DEBIT, 500),
                    EntryDraft(accounts.wallet.id, CREDIT, 400),
                ],
            )

        assert transaction_count(ledger) == 0

    def test_a_transaction_needs_at_least_two_entries(self, ledger, accounts):
        with pytest.raises(errors.InvalidRequest, match="at least two entries"):
            ledger.post(
                kind=TransactionKind.ADJUSTMENT,
                description="Lonely",
                entries=[EntryDraft(accounts.cash.id, DEBIT, 100)],
            )

    @pytest.mark.parametrize("amount", [0, -1, 1.5, "10", True, 10**15 + 1])
    def test_entry_amounts_must_be_positive_integers_in_range(self, ledger, accounts, amount):
        with pytest.raises(errors.InvalidRequest):
            ledger.post(
                kind=TransactionKind.ADJUSTMENT,
                description="Bad amount",
                entries=[
                    EntryDraft(accounts.cash.id, DEBIT, amount),
                    EntryDraft(accounts.wallet.id, CREDIT, amount),
                ],
            )

    def test_a_transaction_needs_a_description(self, ledger, accounts):
        with pytest.raises(errors.InvalidRequest, match="description"):
            ledger.post(
                kind=TransactionKind.ADJUSTMENT,
                description="   ",
                entries=[
                    EntryDraft(accounts.cash.id, DEBIT, 1),
                    EntryDraft(accounts.wallet.id, CREDIT, 1),
                ],
            )

    def test_refuses_an_account_that_does_not_exist(self, ledger, accounts):
        with pytest.raises(errors.AccountNotFound):
            ledger.post(
                kind=TransactionKind.ADJUSTMENT,
                description="Ghost",
                entries=[
                    EntryDraft(accounts.cash.id, DEBIT, 1),
                    EntryDraft(uuid4(), CREDIT, 1),
                ],
            )

    def test_refuses_to_overdraw_and_leaves_every_balance_untouched(self, ledger, accounts):
        deposit(ledger, accounts, 100)

        with pytest.raises(errors.InsufficientFunds):
            withdraw(ledger, accounts, 101)

        assert ledger.balance(accounts.wallet.id).balance_minor == 100
        assert ledger.balance(accounts.cash.id).balance_minor == 100
        assert transaction_count(ledger) == 1

    def test_refuses_to_post_to_a_closed_account(self, ledger, accounts):
        ledger.close_account(accounts.other.id)

        with pytest.raises(errors.AccountClosed):
            deposit(ledger, accounts, 100, to=accounts.other)

    def test_one_account_can_appear_in_several_entries(self, ledger, accounts):
        ledger.post(
            kind=TransactionKind.ADJUSTMENT,
            description="Split deposit",
            entries=[
                EntryDraft(accounts.cash.id, DEBIT, 100),
                EntryDraft(accounts.wallet.id, CREDIT, 60),
                EntryDraft(accounts.wallet.id, CREDIT, 40),
            ],
        )

        assert ledger.balance(accounts.wallet.id).balance_minor == 100


class TestMultiCurrency:
    @pytest.fixture
    def fx(self, ledger):
        def make(code, kind, currency):
            return ledger.open_account(
                code=code,
                name=code,
                account_type=kind,
                currency=currency,
                allow_negative=kind is AccountType.ASSET,
            )

        return SimpleNamespace(
            usd_wallet=make("wallet-usd", AccountType.LIABILITY, "USD"),
            eur_wallet=make("wallet-eur", AccountType.LIABILITY, "EUR"),
            usd_clearing=make("clearing-usd", AccountType.ASSET, "USD"),
            eur_clearing=make("clearing-eur", AccountType.ASSET, "EUR"),
            usd_cash=make("cash-usd", AccountType.ASSET, "USD"),
        )

    def test_a_currency_exchange_balances_in_each_currency(self, ledger, fx):
        ledger.post(
            kind=TransactionKind.DEPOSIT,
            description="Fund",
            entries=[
                EntryDraft(fx.usd_cash.id, DEBIT, 1000),
                EntryDraft(fx.usd_wallet.id, CREDIT, 1000),
            ],
        )

        exchange = ledger.post(
            kind=TransactionKind.ADJUSTMENT,
            description="USD to EUR",
            entries=[
                EntryDraft(fx.usd_wallet.id, DEBIT, 100),
                EntryDraft(fx.usd_clearing.id, CREDIT, 100),
                EntryDraft(fx.eur_clearing.id, DEBIT, 92),
                EntryDraft(fx.eur_wallet.id, CREDIT, 92),
            ],
        )

        assert {e.currency for e in exchange.entries} == {"USD", "EUR"}
        assert ledger.balance(fx.usd_wallet.id).balance_minor == 900
        assert ledger.balance(fx.eur_wallet.id).balance_minor == 92

    def test_an_exchange_that_balances_only_overall_is_refused(self, ledger, fx):
        with pytest.raises(errors.UnbalancedTransaction, match="EUR|USD"):
            ledger.post(
                kind=TransactionKind.ADJUSTMENT,
                description="Cannot net different currencies",
                entries=[
                    EntryDraft(fx.usd_clearing.id, DEBIT, 100),
                    EntryDraft(fx.eur_clearing.id, CREDIT, 100),
                ],
            )


class TestIdempotency:
    def test_repeating_a_request_returns_the_stored_transaction_without_posting_again(
        self, ledger, accounts
    ):
        first = deposit(ledger, accounts, 500, key="k-1")
        second = deposit(ledger, accounts, 500, key="k-1")

        assert first.replayed is False
        assert second.replayed is True
        assert second.id == first.id
        assert ledger.balance(accounts.wallet.id).balance_minor == 500
        assert transaction_count(ledger) == 1

    def test_the_order_of_the_entries_does_not_matter_for_a_replay(self, ledger, accounts):
        entries = [
            EntryDraft(accounts.cash.id, DEBIT, 70),
            EntryDraft(accounts.wallet.id, CREDIT, 70),
        ]
        first = ledger.post(
            kind=TransactionKind.DEPOSIT,
            description="Deposit",
            entries=entries,
            idempotency_key="k",
        )
        second = ledger.post(
            kind=TransactionKind.DEPOSIT,
            description="Deposit",
            entries=list(reversed(entries)),
            idempotency_key="k",
        )

        assert second.replayed is True
        assert second.id == first.id

    def test_reusing_a_key_for_a_different_amount_is_refused(self, ledger, accounts):
        deposit(ledger, accounts, 500, key="k-2")

        with pytest.raises(errors.IdempotencyConflict):
            deposit(ledger, accounts, 501, key="k-2")

    def test_reusing_a_key_for_a_different_account_is_refused(self, ledger, accounts):
        deposit(ledger, accounts, 500, key="k-3")

        with pytest.raises(errors.IdempotencyConflict):
            deposit(ledger, accounts, 500, key="k-3", to=accounts.other)

    def test_a_failed_request_does_not_consume_its_key(self, ledger, accounts):
        with pytest.raises(errors.InsufficientFunds):
            withdraw(ledger, accounts, 100, key="retry-me")
        deposit(ledger, accounts, 100)

        retried = withdraw(ledger, accounts, 100, key="retry-me")

        assert retried.replayed is False
        assert ledger.balance(accounts.wallet.id).balance_minor == 0

    @pytest.mark.parametrize("key", ["", "x" * 201])
    def test_the_key_must_have_a_reasonable_length(self, ledger, accounts, key):
        with pytest.raises(errors.InvalidRequest, match="idempotency key"):
            deposit(ledger, accounts, 100, key=key)


class TestReversal:
    def test_reversing_a_transaction_restores_every_balance(self, ledger, accounts):
        original = deposit(ledger, accounts, 800)

        reversal = ledger.reverse(original.id, reason="Duplicate payment")

        assert reversal.kind is TransactionKind.REVERSAL
        assert reversal.reverses_transaction_id == original.id
        assert ledger.balance(accounts.wallet.id).balance_minor == 0
        assert ledger.balance(accounts.cash.id).balance_minor == 0

    def test_the_reversal_mirrors_each_entry_and_the_original_is_untouched(self, ledger, accounts):
        original = deposit(ledger, accounts, 800)
        reversal = ledger.reverse(original.id, reason="Mistake")

        mirrored = {(e.account_id, e.direction, e.amount_minor) for e in reversal.entries}
        assert mirrored == {
            (accounts.cash.id, CREDIT, 800),
            (accounts.wallet.id, DEBIT, 800),
        }
        assert ledger.get_transaction(original.id).entries == original.entries

    def test_a_transaction_can_only_be_reversed_once(self, ledger, accounts):
        original = deposit(ledger, accounts, 100)
        ledger.reverse(original.id, reason="First")

        with pytest.raises(errors.AlreadyReversed):
            ledger.reverse(original.id, reason="Second")

    def test_reversing_an_unknown_transaction_is_reported(self, ledger):
        with pytest.raises(errors.TransactionNotFound):
            ledger.reverse(uuid4(), reason="Nothing there")

    def test_a_reversal_needs_a_reason(self, ledger, accounts):
        original = deposit(ledger, accounts, 100)

        with pytest.raises(errors.InvalidRequest, match="reason"):
            ledger.reverse(original.id, reason="  ")

    def test_a_reversal_is_refused_when_the_money_has_already_been_spent(self, ledger, accounts):
        original = deposit(ledger, accounts, 100)
        withdraw(ledger, accounts, 100)

        with pytest.raises(errors.InsufficientFunds):
            ledger.reverse(original.id, reason="Too late")

        assert ledger.balance(accounts.wallet.id).balance_minor == 0

    def test_repeating_a_reversal_with_the_same_key_returns_the_first_one(self, ledger, accounts):
        original = deposit(ledger, accounts, 100)
        first = ledger.reverse(original.id, reason="Mistake", idempotency_key="rev-1")
        second = ledger.reverse(original.id, reason="Mistake", idempotency_key="rev-1")

        assert second.replayed is True
        assert second.id == first.id

    def test_reversing_a_reversal_applies_the_original_effect_again(self, ledger, accounts):
        original = deposit(ledger, accounts, 100)
        reversal = ledger.reverse(original.id, reason="Mistake")

        ledger.reverse(reversal.id, reason="It was right after all")

        assert ledger.balance(accounts.wallet.id).balance_minor == 100


class TestStatement:
    @pytest.fixture
    def activity(self, ledger, accounts):
        deposit(ledger, accounts, 1000)
        withdraw(ledger, accounts, 300)
        deposit(ledger, accounts, 50)
        return accounts

    def test_lists_entries_newest_first_with_the_running_balance(self, ledger, activity):
        lines = ledger.statement(activity.wallet.id)

        assert [line.account_seq for line in lines] == [3, 2, 1]
        assert [line.balance_after_minor for line in lines] == [750, 700, 1000]
        assert [line.kind for line in lines] == [
            TransactionKind.DEPOSIT,
            TransactionKind.WITHDRAWAL,
            TransactionKind.DEPOSIT,
        ]

    def test_signs_each_amount_by_its_effect_on_the_account(self, ledger, activity):
        lines = ledger.statement(activity.wallet.id)

        assert [line.signed_amount_minor for line in lines] == [50, -300, 1000]

    def test_pages_backwards_with_the_sequence_number(self, ledger, activity):
        first_page = ledger.statement(activity.wallet.id, limit=2)
        second_page = ledger.statement(
            activity.wallet.id, limit=2, before_seq=first_page[-1].account_seq
        )

        assert [line.account_seq for line in first_page] == [3, 2]
        assert [line.account_seq for line in second_page] == [1]

    @pytest.mark.parametrize("limit", [0, 501])
    def test_the_page_size_is_bounded(self, ledger, activity, limit):
        with pytest.raises(errors.InvalidRequest):
            ledger.statement(activity.wallet.id, limit=limit)

    def test_reports_an_unknown_account(self, ledger):
        with pytest.raises(errors.AccountNotFound):
            ledger.statement(uuid4())


class TestReports:
    def test_the_trial_balance_lists_every_account_and_debits_equal_credits(self, ledger, accounts):
        deposit(ledger, accounts, 1000)
        withdraw(ledger, accounts, 250)

        rows = {row.code: row for row in ledger.trial_balance()}

        assert rows["cash"].debits_minor == 1000
        assert rows["cash"].credits_minor == 250
        assert rows["wallet"].balance_minor == 750
        assert rows["other"].debits_minor == 0
        assert sum(row.debits_minor for row in rows.values()) == sum(
            row.credits_minor for row in rows.values()
        )

    def test_the_integrity_report_is_empty_for_a_consistent_ledger(self, ledger, accounts):
        deposit(ledger, accounts, 1000)
        withdraw(ledger, accounts, 250)
        ledger.reverse(deposit(ledger, accounts, 10).id, reason="Test")

        assert ledger.integrity_report() == []
