from uuid import uuid4

import pytest

from walletledger import errors
from walletledger.fees import FeePolicy
from walletledger.models import AccountType, Direction, TransactionKind
from walletledger.wallets import WalletService


class TestOpeningWallets:
    def test_opens_a_liability_account_owned_by_the_customer(self, wallets):
        wallet = wallets.open_wallet("alice", "usd")

        assert wallet.code == "wallet:alice:USD"
        assert wallet.type is AccountType.LIABILITY
        assert wallet.currency == "USD"
        assert wallet.owner_ref == "alice"
        assert wallet.allow_negative is False

    def test_a_customer_can_have_one_wallet_per_currency(self, wallets):
        usd = wallets.open_wallet("alice", "USD")
        eur = wallets.open_wallet("alice", "EUR")

        assert usd.id != eur.id

    def test_refuses_a_second_wallet_in_the_same_currency(self, wallets):
        wallets.open_wallet("alice", "USD")

        with pytest.raises(errors.DuplicateAccount):
            wallets.open_wallet("alice", "USD")

    @pytest.mark.parametrize("owner_ref", ["", " ", "has space", "a" * 65, "-leading", "ünï"])
    def test_refuses_an_owner_reference_that_is_not_plain(self, wallets, owner_ref):
        with pytest.raises(errors.InvalidRequest, match="owner_ref"):
            wallets.open_wallet(owner_ref, "USD")

    def test_refuses_an_unsupported_currency(self, wallets):
        with pytest.raises(errors.InvalidRequest, match="unsupported currency"):
            wallets.open_wallet("alice", "XXX")

    def test_a_new_wallet_has_no_money(self, wallets):
        wallet = wallets.open_wallet("alice", "USD")

        balance = wallets.balance(wallet.id)

        assert (balance.balance_minor, balance.held_minor, balance.available_minor) == (0, 0, 0)

    def test_an_account_that_is_not_a_wallet_is_not_found_as_one(self, wallets):
        wallets.open_wallet("alice", "USD")
        cash = wallets.ensure_system_accounts("USD").cash

        with pytest.raises(errors.AccountNotFound):
            wallets.get_wallet(cash.id)

    def test_system_accounts_are_created_once_per_currency(self, wallets):
        first = wallets.ensure_system_accounts("USD")
        second = wallets.ensure_system_accounts("usd")

        assert first.cash.id == second.cash.id
        assert first.cash.type is AccountType.ASSET
        assert first.fees.type is AccountType.REVENUE
        assert wallets.ensure_system_accounts("EUR").cash.id != first.cash.id


class TestDepositsAndWithdrawals:
    def test_a_deposit_credits_the_wallet_and_debits_platform_cash(
        self, wallets, ledger, make_wallet
    ):
        wallet = make_wallet()

        posted = wallets.deposit(wallet.id, 2500, idempotency_key="dep-1")

        cash = wallets.ensure_system_accounts("USD").cash
        assert posted.kind is TransactionKind.DEPOSIT
        assert {(e.account_id, e.direction) for e in posted.entries} == {
            (cash.id, Direction.DEBIT),
            (wallet.id, Direction.CREDIT),
        }
        assert wallets.balance(wallet.id).balance_minor == 2500
        assert ledger.balance(cash.id).balance_minor == 2500

    def test_a_withdrawal_debits_the_wallet_and_credits_platform_cash(
        self, wallets, ledger, make_wallet
    ):
        wallet = make_wallet(2500)

        wallets.withdraw(wallet.id, 1000, idempotency_key="wd-1")

        cash = wallets.ensure_system_accounts("USD").cash
        assert wallets.balance(wallet.id).balance_minor == 1500
        assert ledger.balance(cash.id).balance_minor == 1500

    def test_a_withdrawal_cannot_exceed_the_balance(self, wallets, make_wallet):
        wallet = make_wallet(500)

        with pytest.raises(errors.InsufficientFunds):
            wallets.withdraw(wallet.id, 501, idempotency_key="wd-2")

        assert wallets.balance(wallet.id).balance_minor == 500

    def test_repeating_a_deposit_with_the_same_key_credits_only_once(self, wallets, make_wallet):
        wallet = make_wallet()

        first = wallets.deposit(wallet.id, 700, idempotency_key="same")
        second = wallets.deposit(wallet.id, 700, idempotency_key="same")

        assert second.replayed is True
        assert second.id == first.id
        assert wallets.balance(wallet.id).balance_minor == 700

    def test_reusing_a_key_for_another_amount_is_refused(self, wallets, make_wallet):
        wallet = make_wallet()
        wallets.deposit(wallet.id, 700, idempotency_key="same")

        with pytest.raises(errors.IdempotencyConflict):
            wallets.deposit(wallet.id, 800, idempotency_key="same")

    @pytest.mark.parametrize("amount", [0, -5, 1.5, True])
    def test_amounts_must_be_positive_integers(self, wallets, make_wallet, amount):
        wallet = make_wallet()

        with pytest.raises(errors.InvalidRequest):
            wallets.deposit(wallet.id, amount, idempotency_key="bad")

    def test_an_unknown_wallet_is_reported(self, wallets):
        with pytest.raises(errors.AccountNotFound):
            wallets.deposit(uuid4(), 100, idempotency_key="ghost")

    def test_a_closed_wallet_refuses_deposits(self, wallets, ledger, make_wallet):
        wallet = make_wallet()
        ledger.close_account(wallet.id)

        with pytest.raises(errors.AccountClosed):
            wallets.deposit(wallet.id, 100, idempotency_key="closed")

    def test_the_reference_is_kept_with_the_transaction(self, wallets, ledger, make_wallet):
        wallet = make_wallet()

        posted = wallets.deposit(wallet.id, 100, idempotency_key="ref", reference="invoice-7")

        assert ledger.get_transaction(posted.id).metadata["reference"] == "invoice-7"


class TestTransfers:
    def test_moves_money_between_two_wallets(self, wallets, make_wallet):
        sender, receiver = make_wallet(1000), make_wallet()

        posted = wallets.transfer(sender.id, receiver.id, 400, idempotency_key="t-1")

        assert posted.kind is TransactionKind.TRANSFER
        assert wallets.balance(sender.id).balance_minor == 600
        assert wallets.balance(receiver.id).balance_minor == 400

    def test_a_transfer_cannot_exceed_the_senders_balance(self, wallets, make_wallet):
        sender, receiver = make_wallet(100), make_wallet()

        with pytest.raises(errors.InsufficientFunds):
            wallets.transfer(sender.id, receiver.id, 101, idempotency_key="t-2")

        assert wallets.balance(sender.id).balance_minor == 100
        assert wallets.balance(receiver.id).balance_minor == 0

    def test_repeating_a_transfer_with_the_same_key_moves_money_once(self, wallets, make_wallet):
        sender, receiver = make_wallet(1000), make_wallet()

        wallets.transfer(sender.id, receiver.id, 300, idempotency_key="t-3")
        replay = wallets.transfer(sender.id, receiver.id, 300, idempotency_key="t-3")

        assert replay.replayed is True
        assert wallets.balance(sender.id).balance_minor == 700

    def test_refuses_a_transfer_to_the_same_wallet(self, wallets, make_wallet):
        wallet = make_wallet(100)

        with pytest.raises(errors.InvalidRequest, match="two different wallets"):
            wallets.transfer(wallet.id, wallet.id, 10, idempotency_key="t-4")

    def test_refuses_a_transfer_between_currencies(self, wallets, make_wallet):
        dollars, euros = make_wallet(100, "USD"), make_wallet(0, "EUR")

        with pytest.raises(errors.CurrencyMismatch):
            wallets.transfer(dollars.id, euros.id, 10, idempotency_key="t-5")

    def test_refuses_an_unknown_receiver(self, wallets, make_wallet):
        sender = make_wallet(100)

        with pytest.raises(errors.AccountNotFound):
            wallets.transfer(sender.id, uuid4(), 10, idempotency_key="t-6")

    def test_refuses_to_send_to_a_closed_wallet(self, wallets, ledger, make_wallet):
        sender, receiver = make_wallet(100), make_wallet()
        ledger.close_account(receiver.id)

        with pytest.raises(errors.AccountClosed):
            wallets.transfer(sender.id, receiver.id, 10, idempotency_key="t-7")

        assert wallets.balance(sender.id).balance_minor == 100


class TestTransferFees:
    @pytest.fixture
    def charging(self, ledger):
        return WalletService(ledger, FeePolicy(basis_points=250, minimum_minor=30))

    def open(self, service, owner, balance=0):
        wallet = service.open_wallet(owner, "USD")
        if balance:
            service.deposit(wallet.id, balance, idempotency_key=f"fund-{owner}")
        return wallet

    def test_the_sender_pays_the_fee_on_top_of_the_amount(self, charging, ledger):
        sender, receiver = self.open(charging, "a", 10_000), self.open(charging, "b")

        posted = charging.transfer(sender.id, receiver.id, 2_000, idempotency_key="f-1")

        fees = charging.ensure_system_accounts("USD").fees
        assert charging.balance(sender.id).balance_minor == 10_000 - 2_000 - 50
        assert charging.balance(receiver.id).balance_minor == 2_000
        assert ledger.balance(fees.id).balance_minor == 50
        assert len(posted.entries) == 3

    def test_the_minimum_fee_applies_to_small_transfers(self, charging, ledger):
        sender, receiver = self.open(charging, "a", 1_000), self.open(charging, "b")

        charging.transfer(sender.id, receiver.id, 100, idempotency_key="f-2")

        assert ledger.balance(charging.ensure_system_accounts("USD").fees.id).balance_minor == 30

    def test_the_fee_must_also_be_covered_by_the_senders_balance(self, charging):
        sender, receiver = self.open(charging, "a", 2_000), self.open(charging, "b")

        with pytest.raises(errors.InsufficientFunds):
            charging.transfer(sender.id, receiver.id, 2_000, idempotency_key="f-3")

        assert charging.balance(sender.id).balance_minor == 2_000

    def test_the_fee_is_recorded_with_the_transaction(self, charging, ledger):
        sender, receiver = self.open(charging, "a", 10_000), self.open(charging, "b")

        posted = charging.transfer(sender.id, receiver.id, 2_000, idempotency_key="f-4")

        metadata = ledger.get_transaction(posted.id).metadata
        assert (metadata["amount_minor"], metadata["fee_minor"]) == (2_000, 50)

    def test_reversing_a_transfer_gives_the_fee_back_too(self, charging, ledger):
        sender, receiver = self.open(charging, "a", 10_000), self.open(charging, "b")
        posted = charging.transfer(sender.id, receiver.id, 2_000, idempotency_key="f-5")

        charging.reverse_transaction(posted.id, reason="Customer request")

        assert charging.balance(sender.id).balance_minor == 10_000
        assert ledger.balance(charging.ensure_system_accounts("USD").fees.id).balance_minor == 0


class TestReversals:
    def test_reverses_a_transfer(self, wallets, make_wallet):
        sender, receiver = make_wallet(1000), make_wallet()
        posted = wallets.transfer(sender.id, receiver.id, 400, idempotency_key="r-1")

        reversal = wallets.reverse_transaction(posted.id, reason="Sent by mistake")

        assert reversal.kind is TransactionKind.REVERSAL
        assert wallets.balance(sender.id).balance_minor == 1000
        assert wallets.balance(receiver.id).balance_minor == 0

    def test_cannot_reverse_a_transfer_whose_money_was_already_spent(self, wallets, make_wallet):
        sender, receiver = make_wallet(1000), make_wallet()
        posted = wallets.transfer(sender.id, receiver.id, 400, idempotency_key="r-2")
        wallets.withdraw(receiver.id, 400, idempotency_key="r-2-spend")

        with pytest.raises(errors.InsufficientFunds):
            wallets.reverse_transaction(posted.id, reason="Too late")


class TestStatementOfAWallet:
    def test_shows_every_movement_with_its_kind_and_running_balance(
        self, wallets, ledger, make_wallet
    ):
        sender, receiver = make_wallet(1000), make_wallet()
        wallets.transfer(sender.id, receiver.id, 400, idempotency_key="s-1")
        wallets.withdraw(sender.id, 100, idempotency_key="s-2")

        lines = ledger.statement(sender.id)

        assert [
            (line.kind, line.signed_amount_minor, line.balance_after_minor) for line in lines
        ] == [
            (TransactionKind.WITHDRAWAL, -100, 500),
            (TransactionKind.TRANSFER, -400, 600),
            (TransactionKind.DEPOSIT, 1000, 1000),
        ]
