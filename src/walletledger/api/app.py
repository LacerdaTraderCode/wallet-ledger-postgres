from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from walletledger import errors
from walletledger.api import presenters, schemas
from walletledger.database import create_pool, migrate
from walletledger.fees import FeePolicy
from walletledger.ledger import LedgerService
from walletledger.money import parse_amount
from walletledger.settings import Settings
from walletledger.wallets import WalletService

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]

_STATUS_BY_CODE: dict[str, int] = {
    "invalid_request": 400,
    "account_not_found": 404,
    "transaction_not_found": 404,
    "hold_not_found": 404,
    "duplicate_account": 409,
    "idempotency_conflict": 409,
    "already_reversed": 409,
    "hold_not_active": 409,
    "account_closed": 409,
    "account_not_empty": 409,
    "insufficient_funds": 422,
    "currency_mismatch": 422,
    "unbalanced_transaction": 422,
}


def _wallets(request: Request) -> WalletService:
    return request.app.state.wallets


def _ledger(request: Request) -> LedgerService:
    return request.app.state.ledger


Wallets = Annotated[WalletService, Depends(_wallets)]
Ledger = Annotated[LedgerService, Depends(_ledger)]

router = APIRouter()


def _created_or_replayed(response: Response, replayed: bool) -> None:
    response.status_code = 200 if replayed else 201


@router.get("/health")
def health(ledger: Ledger) -> dict[str, str]:
    ledger.execute(lambda conn: conn.execute("SELECT 1").fetchone())
    return {"status": "ok"}


@router.post("/wallets", response_model=schemas.WalletOut, status_code=201)
def open_wallet(body: schemas.WalletCreate, wallets: Wallets) -> schemas.WalletOut:
    account = wallets.open_wallet(body.owner_ref, body.currency)
    return presenters.wallet(account, wallets.balance(account.id))


@router.get("/wallets/{wallet_id}", response_model=schemas.WalletOut)
def get_wallet(wallet_id: UUID, wallets: Wallets) -> schemas.WalletOut:
    return presenters.wallet(wallets.get_wallet(wallet_id), wallets.balance(wallet_id))


@router.get("/wallets/{wallet_id}/statement", response_model=schemas.StatementOut)
def statement(
    wallet_id: UUID,
    wallets: Wallets,
    ledger: Ledger,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
) -> schemas.StatementOut:
    wallets.get_wallet(wallet_id)
    lines = ledger.statement(wallet_id, limit=limit, before_seq=before_seq)
    next_cursor = lines[-1].account_seq if len(lines) == limit else None
    return schemas.StatementOut(
        lines=[presenters.statement_line(line) for line in lines], next_before_seq=next_cursor
    )


@router.post(
    "/wallets/{wallet_id}/deposits", response_model=schemas.TransactionOut, status_code=201
)
def deposit(
    wallet_id: UUID,
    body: schemas.AmountIn,
    idempotency_key: IdempotencyKey,
    response: Response,
    wallets: Wallets,
) -> schemas.TransactionOut:
    wallet = wallets.get_wallet(wallet_id)
    posted = wallets.deposit(
        wallet_id,
        parse_amount(body.amount, wallet.currency),
        idempotency_key=idempotency_key,
        reference=body.reference,
    )
    _created_or_replayed(response, posted.replayed)
    return presenters.transaction(posted)


@router.post(
    "/wallets/{wallet_id}/withdrawals", response_model=schemas.TransactionOut, status_code=201
)
def withdraw(
    wallet_id: UUID,
    body: schemas.AmountIn,
    idempotency_key: IdempotencyKey,
    response: Response,
    wallets: Wallets,
) -> schemas.TransactionOut:
    wallet = wallets.get_wallet(wallet_id)
    posted = wallets.withdraw(
        wallet_id,
        parse_amount(body.amount, wallet.currency),
        idempotency_key=idempotency_key,
        reference=body.reference,
    )
    _created_or_replayed(response, posted.replayed)
    return presenters.transaction(posted)


@router.post("/transfers", response_model=schemas.TransactionOut, status_code=201)
def transfer(
    body: schemas.TransferIn,
    idempotency_key: IdempotencyKey,
    response: Response,
    wallets: Wallets,
) -> schemas.TransactionOut:
    sender = wallets.get_wallet(body.from_wallet_id)
    posted = wallets.transfer(
        body.from_wallet_id,
        body.to_wallet_id,
        parse_amount(body.amount, sender.currency),
        idempotency_key=idempotency_key,
        reference=body.reference,
    )
    _created_or_replayed(response, posted.replayed)
    return presenters.transaction(posted)


@router.post("/wallets/{wallet_id}/holds", response_model=schemas.HoldOut, status_code=201)
def place_hold(
    wallet_id: UUID,
    body: schemas.HoldIn,
    response: Response,
    wallets: Wallets,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=1, max_length=200)
    ] = None,
) -> schemas.HoldOut:
    wallet = wallets.get_wallet(wallet_id)
    placed = wallets.place_hold(
        wallet_id,
        parse_amount(body.amount, wallet.currency),
        idempotency_key=idempotency_key,
        reference=body.reference,
        expires_at=body.expires_at,
    )
    _created_or_replayed(response, placed.replayed)
    return presenters.hold(placed, wallet.currency)


@router.get("/holds/{hold_id}", response_model=schemas.HoldOut)
def get_hold(hold_id: UUID, wallets: Wallets) -> schemas.HoldOut:
    found = wallets.get_hold(hold_id)
    return presenters.hold(found, wallets.get_wallet(found.account_id).currency)


@router.post("/holds/{hold_id}/capture", response_model=schemas.TransactionOut, status_code=201)
def capture_hold(
    hold_id: UUID,
    body: schemas.CaptureIn,
    idempotency_key: IdempotencyKey,
    response: Response,
    wallets: Wallets,
) -> schemas.TransactionOut:
    found = wallets.get_hold(hold_id)
    currency = wallets.get_wallet(found.account_id).currency
    posted = wallets.capture_hold(
        hold_id,
        destination_wallet_id=body.destination_wallet_id,
        idempotency_key=idempotency_key,
        amount_minor=None if body.amount is None else parse_amount(body.amount, currency),
        reference=body.reference,
    )
    _created_or_replayed(response, posted.replayed)
    return presenters.transaction(posted)


@router.post("/holds/{hold_id}/release", response_model=schemas.HoldOut)
def release_hold(hold_id: UUID, wallets: Wallets) -> schemas.HoldOut:
    released = wallets.release_hold(hold_id)
    return presenters.hold(released, wallets.get_wallet(released.account_id).currency)


@router.get("/transactions/{transaction_id}", response_model=schemas.TransactionOut)
def get_transaction(transaction_id: UUID, ledger: Ledger) -> schemas.TransactionOut:
    return presenters.transaction(ledger.get_transaction(transaction_id))


@router.post(
    "/transactions/{transaction_id}/reversal",
    response_model=schemas.TransactionOut,
    status_code=201,
)
def reverse_transaction(
    transaction_id: UUID,
    body: schemas.ReversalIn,
    response: Response,
    wallets: Wallets,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=1, max_length=200)
    ] = None,
) -> schemas.TransactionOut:
    reversed_transaction = wallets.reverse_transaction(
        transaction_id, reason=body.reason, idempotency_key=idempotency_key
    )
    _created_or_replayed(response, reversed_transaction.replayed)
    return presenters.transaction(reversed_transaction)


@router.get("/ledger/trial-balance", response_model=schemas.TrialBalanceOut)
def trial_balance(ledger: Ledger) -> schemas.TrialBalanceOut:
    return presenters.trial_balance(ledger.trial_balance())


@router.get("/ledger/integrity", response_model=schemas.IntegrityOut)
def integrity(ledger: Ledger) -> schemas.IntegrityOut:
    return presenters.integrity(ledger.integrity_report())


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if config.auto_migrate:
            migrate(config.database_url)
        pool = create_pool(
            config.database_url, min_size=config.pool_min_size, max_size=config.pool_max_size
        )
        ledger = LedgerService(pool)
        app.state.ledger = ledger
        app.state.wallets = WalletService(
            ledger,
            FeePolicy(
                basis_points=config.transfer_fee_basis_points,
                minimum_minor=config.transfer_fee_minimum_minor,
            ),
        )
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(
        title="Wallet Ledger",
        version="0.1.0",
        description="Double-entry wallet ledger on PostgreSQL.",
        lifespan=lifespan,
    )
    app.include_router(router)

    @app.exception_handler(errors.LedgerError)
    async def ledger_error_handler(request: Request, exc: errors.LedgerError) -> JSONResponse:
        status = _STATUS_BY_CODE.get(exc.code, 500)
        return JSONResponse(
            status_code=status, content={"error": {"code": exc.code, "message": exc.message}}
        )

    return app
