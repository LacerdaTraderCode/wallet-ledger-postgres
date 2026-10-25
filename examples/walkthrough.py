"""Walks through the main flows on a real database and prints what the ledger recorded.

Run it against an empty PostgreSQL database:

    LEDGER_DATABASE_URL=postgresql://user:password@localhost:5432/walletledger \
        python examples/walkthrough.py
"""

import os

from walletledger.database import create_pool, migrate
from walletledger.fees import FeePolicy
from walletledger.ledger import LedgerService
from walletledger.models import Account
from walletledger.money import format_amount
from walletledger.wallets import WalletService


def show_balances(wallets: WalletService, *accounts: Account) -> None:
    for account in accounts:
        balance = wallets.balance(account.id)
        print(
            f"  {account.owner_ref:<6} balance {format_amount(balance.balance_minor, 'USD'):>8}"
            f"  held {format_amount(balance.held_minor, 'USD'):>7}"
            f"  available {format_amount(balance.available_minor, 'USD'):>8}"
        )


def main() -> None:
    url = os.environ["LEDGER_DATABASE_URL"]
    print("migrations applied:", ", ".join(migrate(url)) or "none (already up to date)")

    pool = create_pool(url)
    ledger = LedgerService(pool)
    # A 1% fee on transfers, never less than 0.05, paid by the sender.
    wallets = WalletService(ledger, FeePolicy(basis_points=100, minimum_minor=5))

    alice, bob, shop = (wallets.open_wallet(owner, "USD") for owner in ("alice", "bob", "shop"))

    print("\n1. Alice deposits 500.00; repeating the request changes nothing")
    first = wallets.deposit(alice.id, 50_000, idempotency_key="demo-deposit")
    repeat = wallets.deposit(alice.id, 50_000, idempotency_key="demo-deposit")
    print(
        f"  first replayed={first.replayed}, repeat replayed={repeat.replayed}, "
        f"same id={first.id == repeat.id}"
    )
    show_balances(wallets, alice)

    print("\n2. Alice sends Bob 100.00 and pays the 1.00 fee on top")
    wallets.transfer(alice.id, bob.id, 10_000, idempotency_key="demo-transfer")
    show_balances(wallets, alice, bob)

    print("\n3. Alice authorizes 150.00 for an order: the money stays put but cannot be spent")
    hold = wallets.place_hold(alice.id, 15_000, reference="order-1001")
    show_balances(wallets, alice)

    print("\n4. The shop captures 120.00 of it; the rest is released")
    wallets.capture_hold(
        hold.id, destination_wallet_id=shop.id, amount_minor=12_000, idempotency_key="demo-capture"
    )
    show_balances(wallets, alice, shop)

    print("\n5. Bob sends the shop 25.00 by mistake and it is reversed, not edited")
    mistaken = wallets.transfer(bob.id, shop.id, 2_500, idempotency_key="demo-mistake")
    wallets.reverse_transaction(mistaken.id, reason="Sent to the wrong merchant")
    show_balances(wallets, bob, shop)

    print("\n6. Alice's statement, newest first, with the balance after each entry")
    for line in ledger.statement(alice.id):
        print(
            f"  #{line.account_seq} {line.kind.value:<12}"
            f" {format_amount(line.signed_amount_minor, line.currency):>9}"
            f"  balance {format_amount(line.balance_after_minor, line.currency):>8}"
        )

    print("\n7. Trial balance: for every currency, debits equal credits")
    rows = ledger.trial_balance()
    for row in rows:
        print(
            f"  {row.code:<22} debits {format_amount(row.debits_minor, row.currency):>9}"
            f"  credits {format_amount(row.credits_minor, row.currency):>9}"
        )
    total_debits = sum(row.debits_minor for row in rows)
    total_credits = sum(row.credits_minor for row in rows)
    print(
        f"  {'TOTAL':<22} debits {format_amount(total_debits, 'USD'):>9}"
        f"  credits {format_amount(total_credits, 'USD'):>9}"
    )

    print("\n8. Integrity audit (recomputes every stored number from the entries)")
    issues = ledger.integrity_report()
    print("  consistent" if not issues else f"  {len(issues)} problem(s): {issues}")

    pool.close()


if __name__ == "__main__":
    main()
