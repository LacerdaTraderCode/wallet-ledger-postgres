# Wallet Ledger — double-entry on PostgreSQL

A wallet service built on a double-entry ledger, with PostgreSQL as the only store. Customers hold money in wallets, move it between each other, reserve it with holds, and get mistakes reversed. The platform can audit itself at any time, recomputing every stored number from the entries.

The interesting part is not the endpoints. It is that **the rules that protect money live in the database**: balanced transactions, no overdraft, an append-only history and the hold invariant are enforced by triggers and constraints, so a bug in the application (or a second application) cannot break them. [`docs/architecture.md`](docs/architecture.md) explains each decision and its limits.

## What it demonstrates

- **Balanced by construction.** Debits equal credits per currency in every transaction, checked by a deferred constraint trigger at commit. A single-sided transaction never gets stored.
- **Append-only history.** Entries and transactions cannot be updated, deleted or truncated. A mistake is fixed with a reversal that mirrors the original.
- **No overdraft, under concurrency.** The trigger that updates a balance takes a row lock, so forty simultaneous withdrawals from a wallet holding ten of them produce exactly ten successes.
- **Holds.** Reserve part of a balance like a card authorization; the invariant `balance >= held >= 0` is enforced in the database. Capture part or all of it, release it, or let it expire.
- **Idempotent writes.** Every money-moving request carries a key; repeating it returns the stored result, and reusing it for a different request is refused.
- **A deadlock found and fixed by test.** A randomized stress run exposed a lock-ordering bug. It is written up in the architecture document, with the regression tests that pin it.
- **An integrity audit** that notices tampering, and tests that tamper on purpose.
- **Money as decimal strings** over HTTP and whole minor units inside, never floats.

## Stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| Database | PostgreSQL 16 (needs 13 or newer: `xid8`, `gen_random_uuid`) |
| Driver | psycopg 3 with `psycopg_pool` |
| API | FastAPI |
| Migrations | Plain SQL files applied by a small runner, guarded by an advisory lock |
| Tests | pytest against a real PostgreSQL |
| Lint and format | ruff |

## Run it

You need Python 3.12 and a PostgreSQL 13 or newer database.

```bash
createdb walletledger
pip install -e ".[dev]"
export LEDGER_DATABASE_URL=postgresql://user:password@localhost:5432/walletledger
uvicorn walletledger.api:create_app --factory --port 8000
```

The app applies the migrations when it starts. Interactive docs are at `http://localhost:8000/docs`.

```bash
curl -s -X POST localhost:8000/wallets -H 'Content-Type: application/json' \
     -d '{"owner_ref": "alice", "currency": "USD"}'

curl -s -X POST localhost:8000/wallets/$WALLET_ID/deposits \
     -H 'Content-Type: application/json' -H 'Idempotency-Key: deposit-1' \
     -d '{"amount": "250.00"}'
```

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LEDGER_DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/walletledger` | Database to use |
| `LEDGER_AUTO_MIGRATE` | `true` | Apply pending migrations at start-up |
| `LEDGER_POOL_MIN_SIZE`, `LEDGER_POOL_MAX_SIZE` | `1`, `10` | Connection pool bounds |
| `LEDGER_TRANSFER_FEE_BPS` | `0` | Fee charged to the sender of a transfer, in basis points (100 = 1%) |
| `LEDGER_TRANSFER_FEE_MINIMUM_MINOR` | `0` | Minimum transfer fee, in minor units |

### Endpoints

| Method and path | Purpose |
|---|---|
| `POST /wallets`, `GET /wallets/{id}` | Open a wallet; read its balance, held and available amounts |
| `GET /wallets/{id}/statement` | Entries newest first, with the balance after each; paged with `limit` and `before_seq` |
| `POST /wallets/{id}/deposits`, `.../withdrawals` | Move money in or out of the platform |
| `POST /transfers` | Move money between two wallets, with the configured fee |
| `POST /wallets/{id}/holds`, `GET /holds/{id}` | Reserve part of a balance |
| `POST /holds/{id}/capture`, `.../release` | Spend a hold, or free it |
| `GET /transactions/{id}`, `POST /transactions/{id}/reversal` | Read a transaction; reverse it |
| `GET /ledger/trial-balance`, `GET /ledger/integrity` | Reports |
| `GET /health` | Liveness, including a database round trip |

Deposits, withdrawals, transfers and captures require an `Idempotency-Key` header. A replay answers `200`, a new request `201`.

## A walkthrough, with real output

`examples/walkthrough.py` runs the main flows against an empty database and prints what the ledger recorded. This is its actual output:

```text
migrations applied: 0001_ledger_core.sql, 0002_holds.sql, 0003_reporting.sql

1. Alice deposits 500.00; repeating the request changes nothing
  first replayed=False, repeat replayed=True, same id=True
  alice  balance   500.00  held    0.00  available   500.00

2. Alice sends Bob 100.00 and pays the 1.00 fee on top
  alice  balance   399.00  held    0.00  available   399.00
  bob    balance   100.00  held    0.00  available   100.00

3. Alice authorizes 150.00 for an order: the money stays put but cannot be spent
  alice  balance   399.00  held  150.00  available   249.00

4. The shop captures 120.00 of it; the rest is released
  alice  balance   277.80  held    0.00  available   277.80
  shop   balance   120.00  held    0.00  available   120.00

5. Bob sends the shop 25.00 by mistake and it is reversed, not edited
  bob    balance   100.00  held    0.00  available   100.00
  shop   balance   120.00  held    0.00  available   120.00

6. Alice's statement, newest first, with the balance after each entry
  #3 hold_capture   -121.20  balance   277.80
  #2 transfer       -101.00  balance   399.00
  #1 deposit         500.00  balance   500.00

7. Trial balance: for every currency, debits equal credits
  system:cash:USD        debits    500.00  credits      0.00
  system:fees:USD        debits      0.25  credits      2.45
  wallet:alice:USD       debits    222.20  credits    500.00
  wallet:bob:USD         debits     25.25  credits    125.25
  wallet:shop:USD        debits     25.00  credits    145.00
  TOTAL                  debits    772.70  credits    772.70

8. Integrity audit (recomputes every stored number from the entries)
  consistent
```

## A real HTTP session

The app was started with `uvicorn` as a separate process, with a 1% transfer fee (minimum 0.05), and driven over HTTP:

```text
GET  /health                      -> (200, {'status': 'ok'})
POST /wallets (alice)             -> 201
POST deposit 250.00               -> 201 replayed: False
POST deposit again, same key      -> 200 replayed: True | same id: True
POST same key, different amount   -> 409 idempotency_conflict
POST transfer 100.00 (1% fee)     -> 201 entries: [('credit', '1.00'), ('credit', '100.00'), ('debit', '101.00')]
POST withdraw 100.01 (has 100.00) -> 422 insufficient_funds
GET  alice                        -> 149.00 available 149.00
GET  trial balance totals         -> [{'currency': 'USD', 'debits': '351.00', 'credits': '351.00', 'balanced': True}]
GET  integrity                    -> {'ok': True, 'issues': []}
```

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The tests need a PostgreSQL superuser. Without configuration they start a throwaway server through the `pgserver` dev dependency. To use your own, set `LEDGER_TEST_ADMIN_URL` (CI points it at a service container). Each test gets a fresh database cloned from a migrated template.

The result of the full run, on PostgreSQL 16.2:

```text
Name                                 Stmts   Miss  Cover   Missing
------------------------------------------------------------------
src/walletledger/__init__.py             1      0   100%
src/walletledger/api/__init__.py         2      0   100%
src/walletledger/api/app.py            115      0   100%
src/walletledger/api/presenters.py      22      0   100%
src/walletledger/api/schemas.py        103      0   100%
src/walletledger/database.py            59      0   100%
src/walletledger/errors.py              31      0   100%
src/walletledger/fees.py                16      0   100%
src/walletledger/ledger.py             179      1    99%   343
src/walletledger/models.py             122      0   100%
src/walletledger/money.py               32      0   100%
src/walletledger/settings.py            15      0   100%
src/walletledger/wallets.py            161      1    99%   277
------------------------------------------------------------------
TOTAL                                  858      2    99%

332 passed, 1 warning in 26.92s
```

What the tests cover is described in [`docs/architecture.md`](docs/architecture.md#testing-strategy). In short: the schema's own refusals in raw SQL, the services, concurrency (including deliberately crossed lock orders), the integrity audit against deliberate corruption, and the HTTP API.

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request to `main`, and can be started by hand. It has two jobs: lint and format check with ruff, and the full test suite with coverage against an official `postgres:16` service container.

## What was not verified

- **The CI workflow itself.** Every command in it was run locally, including from a clean virtual environment installing the package from `pyproject.toml`. Locally the database was PostgreSQL 16.2 embedded through `pgserver`; CI uses the official `postgres:16` Docker image, so its first run is the first time that image is used.
- **Performance.** The tests prove correctness under concurrency, not throughput. The platform cash account is a known hot row (see the limits in the architecture document).
- **Docker.** There is no Dockerfile or compose file, and nothing was run in a container.
- **Other platforms and versions.** Only Linux, Python 3.12 and PostgreSQL 16 were exercised.
- **Authentication.** There is none; the API assumes a gateway in front of it.
- **Two defensive branches** are not covered by tests (listed in the architecture document); the rest of the package is covered.

## License

MIT
