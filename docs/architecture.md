# Architecture

This document explains why the ledger is built the way it is, and where each decision stops working. Start with the README for what it does and how to run it.

## Scope

A wallet service on top of a double-entry ledger, with PostgreSQL as the only store. It moves money between customer wallets and the platform, reserves money with holds, reverses mistakes, and can audit itself.

Out of scope: authentication and authorization (the HTTP API is meant to sit behind a gateway that provides them), foreign-exchange rates (the engine accepts multi-currency transactions that balance per currency, but nothing prices a conversion), reconciliation against a bank, and multi-database sharding.

## Model

Three ideas carry everything else.

**Accounts have a type, and the type decides what "increase" means.** Assets and expenses grow with debits; liabilities, equity and revenue grow with credits. Every account stores its *natural balance*, which is positive when it is on its normal side. A customer wallet is a **liability** account: the platform owes that money to the customer. Money enters and leaves the platform through its **cash** (asset) account, and transfer fees accrue in a **fees** (revenue) account.

**A transaction is a set of entries that balance.** Each entry debits or credits one account by a positive whole number of minor units (cents, never floats). For each currency in a transaction, debits equal credits. A transaction can mention several currencies as long as each balances on its own.

**Nothing is ever edited.** Transactions and entries are append-only. A mistake is corrected by posting a reversal, a new transaction with every entry mirrored, which links back to the original.

```
accounts ──< account_balances (1:1: balance, held, entry count)
    │
    ├──< entries >── transactions  (idempotency key, reverses_transaction_id)
    │
    └──< holds     (amount reserved on an account, status, captured_transaction_id)
```

## Where each rule lives

The rules that protect money are enforced by the database, not by the application. The Python services check the same things first, so callers get clear errors, but the database is the last line of defense and the only one that cannot be bypassed.

| Rule | Mechanism | SQLSTATE |
|---|---|---|
| Entries of a transaction balance, per currency | Deferred constraint trigger on `entries` | `LD002` |
| A transaction has at least two entries | Deferred constraint trigger on `transactions` | `LD005` |
| An account never goes below zero (unless it opted in), counting held money | `BEFORE INSERT` trigger on `entries` and `holds`, under the balance row lock | `LD001` |
| Entry amounts are positive | Checked first in the trigger, and by a `CHECK` | `23514` |
| An entry is in the currency of its account | Composite foreign key `(account_id, currency)` | `23503` |
| Transactions and entries are append-only | `BEFORE UPDATE/DELETE/TRUNCATE` triggers | `LD006` |
| Entries can only be added in the database transaction that created their transaction | `xid8` stored on the transaction, compared in the trigger | `LD004` |
| Nothing is posted to a closed account; a closed account cannot reopen | Triggers on `accounts` and `entries` | `LD003` |
| Code, type, currency and `allow_negative` of an account never change | `BEFORE UPDATE` trigger on `accounts` | `LD007` |
| An account with a balance or active holds cannot be closed | `BEFORE UPDATE` trigger on `accounts` | `LD008` |
| A hold starts active, resolves once, and never changes amount | Triggers on `holds` | `LD009`, `LD007` |

`LD…` codes are custom SQLSTATEs. `database.translate_error` maps them to domain errors (`InsufficientFunds`, `UnbalancedTransaction`, …), so the HTTP layer never parses a message.

The deferred balance check runs at commit by default. After inserting a transaction's entries, the service runs `SET CONSTRAINTS … IMMEDIATE`, so a failure surfaces from the call that caused it, even when the call joined a larger transaction owned by the caller.

## Balances and statements

Every entry carries two numbers set by the trigger that stores it: `account_seq`, the position of the entry in its account's history, and `balance_after_minor`, the account's balance right after it.

Both are assigned while holding the lock on the account's `account_balances` row. That lock serializes every posting to one account, so `account_seq` is a gap-free total order that matches commit order. A plain identity column would not: two sessions can draw numbers in one order and commit in the other, and a reader paging by id could skip a row. Statements page backwards with `account_seq` as a keyset cursor, so a page costs the same no matter how long the history is.

`account_balances` makes reading a balance a single-row lookup and gives the lock above something to hold. It is derived data: the integrity audit recomputes it from the entries.

## Concurrency

PostgreSQL runs at the default `READ COMMITTED`. Correctness does not depend on a stricter isolation level; it depends on three things.

**One lock per account, taken in one order.** Posting to an account updates its balance row, which takes a row lock held until commit. Two requests spending the same wallet therefore queue, and the second sees the first's result. This is why 40 concurrent withdrawals of 100 from a wallet holding 1000 produce exactly ten successes and thirty `InsufficientFunds`, never eleven.

Locks across several accounts can deadlock if two requests take them in different orders. So every operation that touches more than one balance takes them **lowest id first**: `post` sorts its entries, and `LedgerService.lock_balances` takes a set of balance locks in one `ORDER BY account_id FOR UPDATE` statement.

**A bug the tests found.** Capturing a hold used to resolve the hold first (which locks the payer's balance) and post second (which locks the rest in id order). A capture from a high-id wallet to a low-id one crossed paths with a transfer going the other way, and PostgreSQL had to kill one of them after its one-second deadlock timeout. The hold-expiry job had the same shape: one `UPDATE` visiting holds in the order of a hash of their ids.

A randomized stress run found it: in 14 rounds of 300 mixed operations, one operation exhausted its retries. The integrity audit was clean every time, so nothing was ever corrupted, but an operation failed. Both paths now take their balance locks up front, in ascending order. Over 162 rounds (about 48,000 operations) of the same load afterwards there were no failures, and a round dropped from about 10 seconds to about 1.3, because the deadlocks had been costing a second each.

Three regression tests pin it, and each was checked against the old code:

- One runs captures and transfers in opposite directions at once and asserts that no operation ever had to retry. Against the old code it did not finish within 80 seconds, because every deadlock costs a second.
- A deterministic one holds the lowest wallet's lock from a side connection, starts a capture, and then asks for the higher wallet's lock. That only works if the capture has not taken it yet. Against the old code it failed (in the single run I made of it).
- The third does the same for hold expiry, with twelve wallets, and asks for every other wallet's lock. The old code could only pass by starting from the lowest id, which a hash order does about one time in twelve: it failed in five of six runs.

Against the fixed code none of them fails.

**Bounded retries for what remains.** A deadlock victim, a serialization failure, or the loser of a race on an idempotency key is retried from scratch, up to five times with jittered backoff, but only when the service owns the transaction. When the caller passed its own connection, the exception propagates, because a retry would silently restart the caller's work.

## Idempotency

Every money-moving request carries a client-chosen key. The transaction stores the key (unique) and a fingerprint of the request: kind, description, entries, metadata. Then:

- the same key with the same request returns the stored transaction, flagged `replayed`, and posts nothing;
- the same key with a different request is refused (`idempotency_conflict`), because the client is confusing two operations;
- a request that failed rolled back entirely, so its key is free to retry.

Transactions use `INSERT … ON CONFLICT (idempotency_key) DO NOTHING`, which also makes concurrent requests with one key safe: the second waits for the first to commit and then finds its row.

Holds deliberately do not. A `BEFORE INSERT` trigger runs before PostgreSQL detects the conflict, so `ON CONFLICT DO NOTHING` would still have reserved the amount and then thrown the row away. Placing a hold looks the key up first, and a race on the unique constraint raises `IdempotencyRace`, which the retry loop turns into a clean replay. A test forces that race deterministically by blocking one request on another session's uncommitted insert.

Capturing a hold locks the hold row first, then checks whether the key was already used. A duplicate capture request waits on that lock and replays the first one's result instead of failing with "hold is not active".

## Holds

A hold reserves part of a balance, like a card authorization. The invariant `balance >= held >= 0` is enforced in the database, under the same row lock as postings: placing a hold adds to `held_minor` and fails if the available balance would go negative, and every entry is checked against `balance - held`. A hold can therefore never reserve money that is already spoken for, and held money cannot be withdrawn.

A hold is resolved once: captured, released or expired. A capture can take less than the reserved amount, and the remainder is released with it.

Capture resolves the hold *before* it posts, so the reserved amount is free to be debited. The hold records the id of the capturing transaction before that transaction exists, which is why `holds.captured_transaction_id` is a `DEFERRABLE INITIALLY DEFERRED` foreign key.

## Reversals

`reverse` posts the mirror image of a transaction and links it through `reverses_transaction_id`, which is unique, so a transaction can be reversed once. A reversal is a normal transaction and can itself be reversed, which re-applies the original. It is refused if the money has moved on (reversing a deposit after spending it fails with `InsufficientFunds`), rather than driving a balance negative. Reversing a transfer also gives back its fee, because the fee is an entry of the same transaction.

## Integrity audit

`ledger_integrity_report()` recomputes from the entries everything the triggers maintain and returns each disagreement: every transaction balances in each currency, the whole ledger balances per currency, each stored balance equals the sum of its entries, each entry counter and hold total matches, and each entry's `balance_after_minor` equals the running sum. An empty result means consistent.

The triggers make drift impossible through normal use, so the tests corrupt the data on purpose. A superuser disables the triggers (`session_replication_role = replica`) and edits an amount, a balance, a hold total, a counter or a running balance, and the audit must name each one. That is what an audit is for: a restored backup, a manual fix, a bug.

The report scans every entry. It is meant for a scheduled job or an operator, not for a request path.

## Money

Amounts are whole numbers of minor units everywhere inside the service, in `bigint`. Over HTTP they are decimal **strings** (`"10.50"`), parsed against the currency's exponent: a USD amount with three decimals is rejected, not rounded, and a JPY amount has none. Amounts are capped at 10^15 minor units, far below the `bigint` limit, so sums of many entries cannot overflow.

The transfer fee is configured in basis points with an optional minimum, charged to the sender on top of the amount, and rounded up so rounding never costs the platform money.

## HTTP API

The API is a thin layer: parse, call a service, present. Errors carry a stable code:

| Status | Codes |
|---|---|
| 400 | `invalid_request` |
| 404 | `account_not_found`, `transaction_not_found`, `hold_not_found` |
| 409 | `duplicate_account`, `idempotency_conflict`, `already_reversed`, `hold_not_active`, `account_closed`, `account_not_empty` |
| 422 | `insufficient_funds`, `currency_mismatch`, `unbalanced_transaction`, plus FastAPI's own validation errors |

A replayed request answers `200` and a new one `201`, so a client can tell them apart without reading the body.

## Testing strategy

Tests run against a real PostgreSQL, never a mock, because the rules live in the database. Each test gets a fresh database cloned from a migrated template, which takes milliseconds.

| Level | What it proves |
|---|---|
| Schema invariants, in raw SQL | The database refuses every violation by itself, whatever the application does |
| Ledger and wallet services | The use cases, the error translation, idempotency, reversals, statements |
| Concurrency | No interleaving overdraws, double-posts, double-captures or deadlocks; the ledger audits clean afterwards |
| Integrity | The audit notices each kind of drift |
| HTTP API through `TestClient` | Status codes, error bodies, amounts as strings, replays |

Concurrency tests assert outcomes (exactly ten of forty withdrawals succeed) and end by running the audit, because the property that matters is that no interleaving can leave the ledger inconsistent.

## Known limits

- **A hot account is a bottleneck.** The row lock that makes postings safe also serializes them. Every deposit and withdrawal touches the platform's cash account for that currency, so cash throughput is bounded by one row. If it ever matters, split the cash account into several and spread postings across them; the model allows it, the code does not do it.
- **No authentication.** Anyone who can reach the API can move any wallet's money.
- **The audit and the trial balance scan everything.** Fine for a job, wrong for a request path at scale.
- **Reads are not snapshot-consistent across calls.** A balance read and a statement read are two queries.
- **Migrations only go forward.**
- **Performance was not measured.** The tests prove correctness under concurrency; none of them is a benchmark.
- **Two defensive branches are not covered by tests**: the `IdempotencyRace` raised when a conflicting transaction row disappears before it can be read, and the re-raise of a unique violation that is not an idempotency key.
