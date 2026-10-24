import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
from psycopg import Connection

from walletledger import errors
from walletledger.database import IdempotencyRace
from walletledger.fees import FeePolicy
from walletledger.ledger import LedgerService, fingerprint_of
from walletledger.models import (
    Account,
    AccountType,
    Balance,
    Direction,
    EntryDraft,
    Hold,
    Transaction,
    TransactionKind,
)
from walletledger.money import MAX_AMOUNT_MINOR, normalize_currency

HOLD_COLUMNS = (
    "id, account_id, amount_minor, status, reference, expires_at, "
    "captured_transaction_id, created_at, resolved_at"
)

_OWNER_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,63}$")


@dataclass(frozen=True, slots=True)
class SystemAccounts:
    """The platform's own accounts for one currency."""

    cash: Account
    fees: Account


class WalletService:
    """Customer-facing use cases, each one a balanced transaction in the ledger.

    A customer wallet is a liability account: the platform owes that money to the
    customer. Money enters and leaves the platform through its cash account.
    """

    def __init__(self, ledger: LedgerService, fee_policy: FeePolicy | None = None) -> None:
        self._ledger = ledger
        self._fee_policy = fee_policy or FeePolicy()
        self._system_accounts: dict[str, SystemAccounts] = {}
        self._cache_lock = threading.Lock()

    # --- wallets --------------------------------------------------------------------------

    def ensure_system_accounts(
        self, currency: str, conn: Connection | None = None
    ) -> SystemAccounts:
        """The platform's accounts for a currency, created on first use.

        Inside a caller's transaction (`conn`) the accounts are created on that same
        connection, so the call never waits for a second one from the pool, and they are
        not cached because that transaction could still roll back.
        """
        currency = normalize_currency(currency)
        with self._cache_lock:
            cached = self._system_accounts.get(currency)
        if cached is not None:
            return cached

        accounts = SystemAccounts(
            cash=self._ledger.ensure_account(
                code=f"system:cash:{currency}",
                name=f"Platform cash ({currency})",
                account_type=AccountType.ASSET,
                currency=currency,
                conn=conn,
            ),
            fees=self._ledger.ensure_account(
                code=f"system:fees:{currency}",
                name=f"Transfer fees ({currency})",
                account_type=AccountType.REVENUE,
                currency=currency,
                conn=conn,
            ),
        )
        if conn is None:
            with self._cache_lock:
                self._system_accounts[currency] = accounts
        return accounts

    def open_wallet(self, owner_ref: str, currency: str) -> Account:
        if not _OWNER_REF.fullmatch(owner_ref or ""):
            raise errors.InvalidRequest(
                "owner_ref must have 1 to 64 letters, digits or the characters _ . @ : -"
            )
        currency = normalize_currency(currency)
        self.ensure_system_accounts(currency)
        return self._ledger.open_account(
            code=f"wallet:{owner_ref}:{currency}",
            name=f"Wallet of {owner_ref} ({currency})",
            account_type=AccountType.LIABILITY,
            currency=currency,
            owner_ref=owner_ref,
        )

    def get_wallet(self, wallet_id: UUID, conn: Connection | None = None) -> Account:
        account = self._ledger.get_account(wallet_id, conn)
        if account.type is not AccountType.LIABILITY or account.owner_ref is None:
            raise errors.AccountNotFound(f"wallet {wallet_id} does not exist")
        return account

    def balance(self, wallet_id: UUID) -> Balance:
        self.get_wallet(wallet_id)
        return self._ledger.balance(wallet_id)

    # --- money movement -------------------------------------------------------------------

    def deposit(
        self,
        wallet_id: UUID,
        amount_minor: int,
        *,
        idempotency_key: str,
        reference: str | None = None,
    ) -> Transaction:
        _require_amount(amount_minor)
        wallet = self.get_wallet(wallet_id)
        system = self.ensure_system_accounts(wallet.currency)
        return self._ledger.post(
            kind=TransactionKind.DEPOSIT,
            description=f"Deposit to {wallet.code}",
            entries=[
                EntryDraft(system.cash.id, Direction.DEBIT, amount_minor),
                EntryDraft(wallet.id, Direction.CREDIT, amount_minor),
            ],
            idempotency_key=idempotency_key,
            metadata={"wallet_id": wallet.id, "reference": reference},
        )

    def withdraw(
        self,
        wallet_id: UUID,
        amount_minor: int,
        *,
        idempotency_key: str,
        reference: str | None = None,
    ) -> Transaction:
        _require_amount(amount_minor)
        wallet = self.get_wallet(wallet_id)
        system = self.ensure_system_accounts(wallet.currency)
        return self._ledger.post(
            kind=TransactionKind.WITHDRAWAL,
            description=f"Withdrawal from {wallet.code}",
            entries=[
                EntryDraft(wallet.id, Direction.DEBIT, amount_minor),
                EntryDraft(system.cash.id, Direction.CREDIT, amount_minor),
            ],
            idempotency_key=idempotency_key,
            metadata={"wallet_id": wallet.id, "reference": reference},
        )

    def transfer(
        self,
        from_wallet_id: UUID,
        to_wallet_id: UUID,
        amount_minor: int,
        *,
        idempotency_key: str,
        reference: str | None = None,
    ) -> Transaction:
        _require_amount(amount_minor)
        if from_wallet_id == to_wallet_id:
            raise errors.InvalidRequest("a transfer needs two different wallets")
        sender = self.get_wallet(from_wallet_id)
        receiver = self.get_wallet(to_wallet_id)
        _require_same_currency(sender, receiver)

        return self._ledger.post(
            kind=TransactionKind.TRANSFER,
            description=f"Transfer from {sender.code} to {receiver.code}",
            entries=self._transfer_entries(sender, receiver, amount_minor),
            idempotency_key=idempotency_key,
            metadata=self._transfer_metadata(sender, receiver, amount_minor, reference),
        )

    def reverse_transaction(
        self, transaction_id: UUID, *, reason: str, idempotency_key: str | None = None
    ) -> Transaction:
        return self._ledger.reverse(transaction_id, reason=reason, idempotency_key=idempotency_key)

    def _transfer_entries(
        self,
        sender: Account,
        receiver: Account,
        amount_minor: int,
        conn: Connection | None = None,
    ) -> list[EntryDraft]:
        fee_minor = self._fee_policy.fee_for(amount_minor)
        entries = [
            EntryDraft(sender.id, Direction.DEBIT, amount_minor + fee_minor),
            EntryDraft(receiver.id, Direction.CREDIT, amount_minor),
        ]
        if fee_minor > 0:
            fees = self.ensure_system_accounts(sender.currency, conn).fees
            entries.append(EntryDraft(fees.id, Direction.CREDIT, fee_minor))
        return entries

    def _transfer_metadata(
        self, sender: Account, receiver: Account, amount_minor: int, reference: str | None
    ) -> dict[str, object]:
        return {
            "from_wallet_id": sender.id,
            "to_wallet_id": receiver.id,
            "amount_minor": amount_minor,
            "fee_minor": self._fee_policy.fee_for(amount_minor),
            "reference": reference,
        }

    # --- holds ----------------------------------------------------------------------------

    def place_hold(
        self,
        wallet_id: UUID,
        amount_minor: int,
        *,
        idempotency_key: str | None = None,
        reference: str | None = None,
        expires_at: datetime | None = None,
    ) -> Hold:
        """Reserve part of a wallet's balance. The money stays put but can no longer be spent."""
        _require_amount(amount_minor)
        if expires_at is not None and expires_at.tzinfo is None:
            raise errors.InvalidRequest("expires_at must include a time zone")
        if idempotency_key is not None and not 1 <= len(idempotency_key) <= 200:
            raise errors.InvalidRequest("an idempotency key must have between 1 and 200 characters")
        self.get_wallet(wallet_id)

        fingerprint = None
        if idempotency_key is not None:
            fingerprint = fingerprint_of(
                {
                    "wallet_id": wallet_id,
                    "amount_minor": amount_minor,
                    "reference": reference,
                    "expires_at": expires_at,
                }
            )

        def operation(conn: Connection) -> Hold:
            # Not ON CONFLICT: the BEFORE INSERT trigger would already have reserved the
            # amount by the time the conflict is detected.
            if idempotency_key is not None:
                existing = conn.execute(
                    f"SELECT {HOLD_COLUMNS}, request_fingerprint FROM holds "
                    "WHERE idempotency_key = %s",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if existing["request_fingerprint"] != fingerprint:
                        raise errors.IdempotencyConflict(
                            f"idempotency key {idempotency_key!r} was already used "
                            "with a different request"
                        )
                    return Hold.from_row(existing, replayed=True)
            try:
                row = conn.execute(
                    "INSERT INTO holds (account_id, amount_minor, reference, idempotency_key, "
                    "request_fingerprint, expires_at) VALUES (%s, %s, %s, %s, %s, %s) "
                    f"RETURNING {HOLD_COLUMNS}",
                    (wallet_id, amount_minor, reference, idempotency_key, fingerprint, expires_at),
                ).fetchone()
            except psycopg.errors.UniqueViolation as exc:
                if exc.diag.constraint_name == "holds_idempotency_key_key":
                    raise IdempotencyRace() from exc
                raise
            return Hold.from_row(row)

        return self._ledger.execute(operation)

    def get_hold(self, hold_id: UUID) -> Hold:
        def operation(conn: Connection) -> Hold:
            row = conn.execute(
                f"SELECT {HOLD_COLUMNS} FROM holds WHERE id = %s", (hold_id,)
            ).fetchone()
            if row is None:
                raise errors.HoldNotFound(f"hold {hold_id} does not exist")
            return Hold.from_row(row)

        return self._ledger.execute(operation)

    def release_hold(self, hold_id: UUID) -> Hold:
        return self._resolve_without_posting(hold_id, "released")

    def expire_due_holds(self, now: datetime | None = None) -> int:
        """Release every active hold whose expiry has passed. Returns how many were released."""
        moment = now or datetime.now(UTC)

        def operation(conn: Connection) -> int:
            # Lock the due holds and release them one by one in account order, so the balance
            # locks they take follow the same ascending order as every other operation.
            # SKIP LOCKED leaves holds that someone is capturing or releasing to that someone.
            due = conn.execute(
                "SELECT id FROM holds WHERE status = 'active' AND expires_at IS NOT NULL "
                "AND expires_at <= %s ORDER BY account_id, id FOR UPDATE SKIP LOCKED",
                (moment,),
            ).fetchall()
            for row in due:
                conn.execute(
                    "UPDATE holds SET status = 'expired', resolved_at = now() WHERE id = %s",
                    (row["id"],),
                )
            return len(due)

        return self._ledger.execute(operation)

    def capture_hold(
        self,
        hold_id: UUID,
        *,
        destination_wallet_id: UUID,
        idempotency_key: str,
        amount_minor: int | None = None,
        reference: str | None = None,
    ) -> Transaction:
        """Spend a hold: move up to its amount to another wallet. Any remainder is released."""

        def operation(conn: Connection) -> Transaction:
            hold_row = conn.execute(
                f"SELECT {HOLD_COLUMNS} FROM holds WHERE id = %s FOR UPDATE", (hold_id,)
            ).fetchone()
            if hold_row is None:
                raise errors.HoldNotFound(f"hold {hold_id} does not exist")
            hold = Hold.from_row(hold_row)

            sender = self.get_wallet(hold.account_id, conn)
            receiver = self.get_wallet(destination_wallet_id, conn)
            if sender.id == receiver.id:
                raise errors.InvalidRequest("a hold cannot be captured into its own wallet")
            _require_same_currency(sender, receiver)

            captured = hold.amount_minor if amount_minor is None else amount_minor
            _require_amount(captured)
            if captured > hold.amount_minor:
                raise errors.InvalidRequest("a hold cannot be captured for more than it reserved")

            entries = self._transfer_entries(sender, receiver, captured, conn)
            metadata = {
                **self._transfer_metadata(sender, receiver, captured, reference),
                "hold_id": hold.id,
            }
            description = f"Capture of hold {hold.id} from {sender.code} to {receiver.code}"

            # The lock on the hold row is held, so a second capture with the same key waits
            # here and then finds the first one's transaction instead of repeating it.
            if self._ledger.idempotency_key_exists(conn, idempotency_key):
                return self._ledger.post(
                    kind=TransactionKind.HOLD_CAPTURE,
                    description=description,
                    entries=entries,
                    idempotency_key=idempotency_key,
                    metadata=metadata,
                    conn=conn,
                )

            if hold.status.value != "active":
                raise errors.HoldNotActive(f"hold {hold.id} is already {hold.status.value}")

            # Resolving the hold locks the payer's balance, and posting locks every balance
            # involved. Taking all of them up front, in id order, keeps this transaction from
            # crossing paths with a transfer that locks the same wallets in the other order.
            self._ledger.lock_balances(conn, {entry.account_id for entry in entries})

            # Resolve the hold first, so the amount it reserved is free to be debited; the
            # foreign key to the transaction is deferred until commit for this reason.
            transaction_id = uuid4()
            conn.execute(
                "UPDATE holds SET status = 'captured', resolved_at = now(), "
                "captured_transaction_id = %s WHERE id = %s",
                (transaction_id, hold.id),
            )
            return self._ledger.post(
                kind=TransactionKind.HOLD_CAPTURE,
                description=description,
                entries=entries,
                idempotency_key=idempotency_key,
                metadata=metadata,
                transaction_id=transaction_id,
                conn=conn,
            )

        return self._ledger.execute(operation)

    def _resolve_without_posting(self, hold_id: UUID, new_status: str) -> Hold:
        def operation(conn: Connection) -> Hold:
            row = conn.execute(
                f"UPDATE holds SET status = %s, resolved_at = now() WHERE id = %s "
                f"RETURNING {HOLD_COLUMNS}",
                (new_status, hold_id),
            ).fetchone()
            if row is None:
                raise errors.HoldNotFound(f"hold {hold_id} does not exist")
            return Hold.from_row(row)

        return self._ledger.execute(operation)


def _require_amount(amount_minor: int) -> None:
    if (
        not isinstance(amount_minor, int)
        or isinstance(amount_minor, bool)
        or not 0 < amount_minor <= MAX_AMOUNT_MINOR
    ):
        raise errors.InvalidRequest("amount must be a positive integer in minor units")


def _require_same_currency(first: Account, second: Account) -> None:
    if first.currency != second.currency:
        raise errors.CurrencyMismatch(
            f"wallets are in different currencies ({first.currency} and {second.currency})"
        )
