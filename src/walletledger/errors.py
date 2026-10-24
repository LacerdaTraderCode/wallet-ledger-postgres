class LedgerError(Exception):
    """Base class for every error the ledger raises on purpose.

    `code` is stable and machine readable; the message is for people.
    """

    code = "ledger_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidRequest(LedgerError):
    code = "invalid_request"


class AccountNotFound(LedgerError):
    code = "account_not_found"


class TransactionNotFound(LedgerError):
    code = "transaction_not_found"


class HoldNotFound(LedgerError):
    code = "hold_not_found"


class DuplicateAccount(LedgerError):
    code = "duplicate_account"


class CurrencyMismatch(LedgerError):
    code = "currency_mismatch"


class UnbalancedTransaction(LedgerError):
    code = "unbalanced_transaction"


class InsufficientFunds(LedgerError):
    code = "insufficient_funds"


class AccountClosed(LedgerError):
    code = "account_closed"


class AccountNotEmpty(LedgerError):
    code = "account_not_empty"


class IdempotencyConflict(LedgerError):
    code = "idempotency_conflict"


class AlreadyReversed(LedgerError):
    code = "already_reversed"


class HoldNotActive(LedgerError):
    code = "hold_not_active"
