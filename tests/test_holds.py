from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from walletledger import errors
from walletledger.fees import FeePolicy
from walletledger.models import HoldStatus, TransactionKind
from walletledger.wallets import WalletService


class TestPlacingHolds:
    def test_a_hold_reserves_money_without_moving_it(self, wallets, make_wallet):
        wallet = make_wallet(1000)

        hold = wallets.place_hold(wallet.id, 300, reference="order-9")

        balance = wallets.balance(wallet.id)
        assert hold.status is HoldStatus.ACTIVE
        assert hold.amount_minor == 300
        assert hold.reference == "order-9"
        assert (balance.balance_minor, balance.held_minor, balance.available_minor) == (
            1000,
            300,
            700,
        )

    def test_cannot_hold_more_than_the_available_balance(self, wallets, make_wallet):
        wallet = make_wallet(1000)
        wallets.place_hold(wallet.id, 800)

        with pytest.raises(errors.InsufficientFunds):
            wallets.place_hold(wallet.id, 201)

        assert wallets.balance(wallet.id).held_minor == 800

    def test_held_money_cannot_be_withdrawn_or_transferred(self, wallets, make_wallet):
        wallet, other = make_wallet(1000), make_wallet()
        wallets.place_hold(wallet.id, 800)

        with pytest.raises(errors.InsufficientFunds):
            wallets.withdraw(wallet.id, 201, idempotency_key="w")
        with pytest.raises(errors.InsufficientFunds):
            wallets.transfer(wallet.id, other.id, 201, idempotency_key="t")

        wallets.withdraw(wallet.id, 200, idempotency_key="w-ok")
        assert wallets.balance(wallet.id).balance_minor == 800

    def test_a_hold_needs_a_positive_amount_and_an_existing_wallet(self, wallets, make_wallet):
        wallet = make_wallet(100)

        with pytest.raises(errors.InvalidRequest):
            wallets.place_hold(wallet.id, 0)
        with pytest.raises(errors.AccountNotFound):
            wallets.place_hold(uuid4(), 10)

    def test_the_expiry_must_carry_a_time_zone(self, wallets, make_wallet):
        wallet = make_wallet(100)

        with pytest.raises(errors.InvalidRequest, match="time zone"):
            wallets.place_hold(wallet.id, 10, expires_at=datetime(2030, 1, 1))

    def test_a_closed_wallet_cannot_receive_a_hold(self, wallets, ledger, make_wallet):
        wallet = make_wallet()
        ledger.close_account(wallet.id)

        with pytest.raises(errors.AccountClosed):
            wallets.place_hold(wallet.id, 10)

    def test_a_wallet_with_an_active_hold_cannot_be_closed(self, wallets, ledger, make_wallet):
        wallet = make_wallet(100)
        wallets.place_hold(wallet.id, 50)

        with pytest.raises(errors.AccountNotEmpty):
            ledger.close_account(wallet.id)

    def test_a_hold_can_be_read_back(self, wallets, make_wallet):
        wallet = make_wallet(100)
        hold = wallets.place_hold(wallet.id, 50)

        assert wallets.get_hold(hold.id) == hold
        with pytest.raises(errors.HoldNotFound):
            wallets.get_hold(uuid4())


class TestHoldIdempotency:
    def test_repeating_a_hold_with_the_same_key_reserves_only_once(self, wallets, make_wallet):
        wallet = make_wallet(1000)

        first = wallets.place_hold(wallet.id, 300, idempotency_key="h-1")
        second = wallets.place_hold(wallet.id, 300, idempotency_key="h-1")

        assert second.replayed is True
        assert second.id == first.id
        assert wallets.balance(wallet.id).held_minor == 300

    def test_reusing_a_key_for_another_amount_is_refused(self, wallets, make_wallet):
        wallet = make_wallet(1000)
        wallets.place_hold(wallet.id, 300, idempotency_key="h-2")

        with pytest.raises(errors.IdempotencyConflict):
            wallets.place_hold(wallet.id, 301, idempotency_key="h-2")

        assert wallets.balance(wallet.id).held_minor == 300


class TestReleasingHolds:
    def test_releasing_frees_the_reserved_money(self, wallets, make_wallet):
        wallet = make_wallet(1000)
        hold = wallets.place_hold(wallet.id, 600)

        released = wallets.release_hold(hold.id)

        assert released.status is HoldStatus.RELEASED
        assert released.resolved_at is not None
        assert wallets.balance(wallet.id).available_minor == 1000

    def test_a_hold_can_only_be_resolved_once(self, wallets, make_wallet):
        wallet = make_wallet(1000)
        hold = wallets.place_hold(wallet.id, 600)
        wallets.release_hold(hold.id)

        with pytest.raises(errors.HoldNotActive):
            wallets.release_hold(hold.id)

    def test_releasing_an_unknown_hold_is_reported(self, wallets):
        with pytest.raises(errors.HoldNotFound):
            wallets.release_hold(uuid4())


class TestCapturingHolds:
    def test_capturing_moves_the_reserved_money_to_the_destination(self, wallets, make_wallet):
        payer, merchant = make_wallet(1000), make_wallet()
        hold = wallets.place_hold(payer.id, 600)

        posted = wallets.capture_hold(
            hold.id, destination_wallet_id=merchant.id, idempotency_key="c-1"
        )

        payer_balance = wallets.balance(payer.id)
        assert posted.kind is TransactionKind.HOLD_CAPTURE
        assert (payer_balance.balance_minor, payer_balance.held_minor) == (400, 0)
        assert wallets.balance(merchant.id).balance_minor == 600
        captured = wallets.get_hold(hold.id)
        assert captured.status is HoldStatus.CAPTURED
        assert captured.captured_transaction_id == posted.id

    def test_a_partial_capture_releases_the_remainder(self, wallets, make_wallet):
        payer, merchant = make_wallet(1000), make_wallet()
        hold = wallets.place_hold(payer.id, 600)

        wallets.capture_hold(
            hold.id, destination_wallet_id=merchant.id, idempotency_key="c-2", amount_minor=250
        )

        balance = wallets.balance(payer.id)
        assert (balance.balance_minor, balance.held_minor, balance.available_minor) == (750, 0, 750)
        assert wallets.balance(merchant.id).balance_minor == 250

    def test_cannot_capture_more_than_was_reserved(self, wallets, make_wallet):
        payer, merchant = make_wallet(1000), make_wallet()
        hold = wallets.place_hold(payer.id, 600)

        with pytest.raises(errors.InvalidRequest, match="more than it reserved"):
            wallets.capture_hold(
                hold.id, destination_wallet_id=merchant.id, idempotency_key="c-3", amount_minor=601
            )

        assert wallets.get_hold(hold.id).status is HoldStatus.ACTIVE
        assert wallets.balance(payer.id).held_minor == 600

    def test_a_hold_cannot_be_captured_twice_under_different_keys(self, wallets, make_wallet):
        payer, merchant = make_wallet(1000), make_wallet()
        hold = wallets.place_hold(payer.id, 600)
        wallets.capture_hold(hold.id, destination_wallet_id=merchant.id, idempotency_key="c-4a")

        with pytest.raises(errors.HoldNotActive):
            wallets.capture_hold(hold.id, destination_wallet_id=merchant.id, idempotency_key="c-4b")

        assert wallets.balance(merchant.id).balance_minor == 600

    def test_repeating_a_capture_with_the_same_key_returns_the_first_result(
        self, wallets, make_wallet
    ):
        payer, merchant = make_wallet(1000), make_wallet()
        hold = wallets.place_hold(payer.id, 600)

        first = wallets.capture_hold(
            hold.id, destination_wallet_id=merchant.id, idempotency_key="c-5"
        )
        second = wallets.capture_hold(
            hold.id, destination_wallet_id=merchant.id, idempotency_key="c-5"
        )

        assert second.replayed is True
        assert second.id == first.id
        assert wallets.balance(merchant.id).balance_minor == 600

    def test_a_released_hold_cannot_be_captured(self, wallets, make_wallet):
        payer, merchant = make_wallet(1000), make_wallet()
        hold = wallets.place_hold(payer.id, 600)
        wallets.release_hold(hold.id)

        with pytest.raises(errors.HoldNotActive):
            wallets.capture_hold(hold.id, destination_wallet_id=merchant.id, idempotency_key="c-6")

    def test_refuses_to_capture_into_the_holders_own_wallet(self, wallets, make_wallet):
        payer = make_wallet(1000)
        hold = wallets.place_hold(payer.id, 600)

        with pytest.raises(errors.InvalidRequest, match="own wallet"):
            wallets.capture_hold(hold.id, destination_wallet_id=payer.id, idempotency_key="c-7")

    def test_refuses_to_capture_into_a_wallet_of_another_currency(self, wallets, make_wallet):
        payer, euros = make_wallet(1000), make_wallet(0, "EUR")
        hold = wallets.place_hold(payer.id, 600)

        with pytest.raises(errors.CurrencyMismatch):
            wallets.capture_hold(hold.id, destination_wallet_id=euros.id, idempotency_key="c-8")

    def test_capturing_an_unknown_hold_is_reported(self, wallets, make_wallet):
        with pytest.raises(errors.HoldNotFound):
            wallets.capture_hold(
                uuid4(), destination_wallet_id=make_wallet().id, idempotency_key="c-9"
            )

    def test_a_failed_capture_leaves_the_hold_active(self, wallets, ledger, make_wallet):
        payer, merchant = make_wallet(1000), make_wallet()
        hold = wallets.place_hold(payer.id, 600)
        ledger.close_account(merchant.id)

        with pytest.raises(errors.AccountClosed):
            wallets.capture_hold(hold.id, destination_wallet_id=merchant.id, idempotency_key="c-10")

        assert wallets.get_hold(hold.id).status is HoldStatus.ACTIVE
        assert wallets.balance(payer.id).held_minor == 600

    def test_the_transfer_fee_applies_to_a_capture(self, ledger):
        charging = WalletService(ledger, FeePolicy(basis_points=100))
        payer, merchant = (
            charging.open_wallet("payer", "USD"),
            charging.open_wallet("merchant", "USD"),
        )
        charging.deposit(payer.id, 10_000, idempotency_key="fund")
        hold = charging.place_hold(payer.id, 5_000)

        charging.capture_hold(hold.id, destination_wallet_id=merchant.id, idempotency_key="cf")

        # 5,000 reserved is not enough for 5,000 + a 50 fee, but the rest of the balance is.
        assert charging.balance(payer.id).balance_minor == 10_000 - 5_000 - 50
        assert charging.balance(merchant.id).balance_minor == 5_000
        assert ledger.balance(charging.ensure_system_accounts("USD").fees.id).balance_minor == 50


class TestCapturingWithAFreshService:
    def test_a_service_that_never_saw_the_currency_can_capture_a_hold(
        self, ledger, wallets, make_wallet
    ):
        payer, merchant = make_wallet(1000), make_wallet()
        hold = wallets.place_hold(payer.id, 600)
        fresh = WalletService(ledger, FeePolicy(basis_points=100))

        posted = fresh.capture_hold(
            hold.id, destination_wallet_id=merchant.id, idempotency_key="fresh"
        )

        assert len(posted.entries) == 3
        assert fresh.balance(payer.id).balance_minor == 1000 - 600 - 6


class TestExpiringHolds:
    def test_expires_only_the_holds_that_are_due(self, wallets, make_wallet):
        wallet = make_wallet(1000)
        now = datetime.now(UTC)
        overdue = wallets.place_hold(wallet.id, 100, expires_at=now - timedelta(minutes=5))
        later = wallets.place_hold(wallet.id, 200, expires_at=now + timedelta(hours=1))
        forever = wallets.place_hold(wallet.id, 300)

        expired = wallets.expire_due_holds(now)

        assert expired == 1
        assert wallets.get_hold(overdue.id).status is HoldStatus.EXPIRED
        assert wallets.get_hold(later.id).status is HoldStatus.ACTIVE
        assert wallets.get_hold(forever.id).status is HoldStatus.ACTIVE
        assert wallets.balance(wallet.id).held_minor == 500

    def test_running_it_twice_expires_nothing_new(self, wallets, make_wallet):
        wallet = make_wallet(1000)
        now = datetime.now(UTC)
        wallets.place_hold(wallet.id, 100, expires_at=now - timedelta(minutes=5))

        assert wallets.expire_due_holds(now) == 1
        assert wallets.expire_due_holds(now) == 0

    def test_an_expired_hold_can_no_longer_be_captured(self, wallets, make_wallet):
        payer, merchant = make_wallet(1000), make_wallet()
        now = datetime.now(UTC)
        hold = wallets.place_hold(payer.id, 100, expires_at=now - timedelta(minutes=1))
        wallets.expire_due_holds(now)

        with pytest.raises(errors.HoldNotActive):
            wallets.capture_hold(hold.id, destination_wallet_id=merchant.id, idempotency_key="late")
