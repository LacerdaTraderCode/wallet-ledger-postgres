from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from walletledger.api import create_app
from walletledger.settings import Settings


@pytest.fixture
def client(database_url):
    settings = Settings(database_url=database_url, auto_migrate=False, pool_max_size=8)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def charging_client(database_url):
    settings = Settings(
        database_url=database_url,
        auto_migrate=False,
        pool_max_size=8,
        transfer_fee_basis_points=100,
        transfer_fee_minimum_minor=5,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def new_key():
    return {"Idempotency-Key": uuid4().hex}


def open_wallet(client, owner, currency="USD"):
    response = client.post("/wallets", json={"owner_ref": owner, "currency": currency})
    assert response.status_code == 201, response.text
    return response.json()


def deposit(client, wallet_id, amount, headers=None):
    return client.post(
        f"/wallets/{wallet_id}/deposits", json={"amount": amount}, headers=headers or new_key()
    )


def funded_wallet(client, owner, amount="100.00", currency="USD"):
    wallet = open_wallet(client, owner, currency)
    assert deposit(client, wallet["id"], amount).status_code == 201
    return wallet


def balance_of(client, wallet_id):
    return client.get(f"/wallets/{wallet_id}").json()


def test_health_checks_the_database(client):
    assert client.get("/health").json() == {"status": "ok"}


class TestWallets:
    def test_opening_a_wallet_returns_it_with_a_zero_balance(self, client):
        response = client.post("/wallets", json={"owner_ref": "alice", "currency": "USD"})

        assert response.status_code == 201
        body = response.json()
        assert body["owner_ref"] == "alice"
        assert body["currency"] == "USD"
        assert body["balance"] == {"amount": "0.00", "amount_minor": 0, "currency": "USD"}
        assert body["available"]["amount"] == "0.00"
        assert body["is_closed"] is False

    def test_a_second_wallet_in_the_same_currency_is_a_conflict(self, client):
        open_wallet(client, "alice")

        response = client.post("/wallets", json={"owner_ref": "alice", "currency": "USD"})

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "duplicate_account"

    def test_an_unsupported_currency_is_a_bad_request(self, client):
        response = client.post("/wallets", json={"owner_ref": "alice", "currency": "XXX"})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"

    def test_a_body_with_missing_fields_is_rejected(self, client):
        assert client.post("/wallets", json={"currency": "USD"}).status_code == 422

    def test_reading_a_wallet(self, client):
        wallet = open_wallet(client, "alice")

        assert client.get(f"/wallets/{wallet['id']}").json() == wallet

    def test_an_unknown_wallet_is_not_found(self, client):
        response = client.get(f"/wallets/{uuid4()}")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "account_not_found"

    def test_a_malformed_id_is_a_validation_error(self, client):
        assert client.get("/wallets/not-a-uuid").status_code == 422

    def test_amounts_follow_the_minor_units_of_the_currency(self, client):
        yen = open_wallet(client, "kenji", "JPY")

        assert deposit(client, yen["id"], "500").status_code == 201
        assert balance_of(client, yen["id"])["balance"]["amount"] == "500"
        assert deposit(client, yen["id"], "1.5").status_code == 400


class TestDepositsAndWithdrawals:
    def test_a_deposit_returns_the_posted_transaction(self, client):
        wallet = open_wallet(client, "alice")

        response = deposit(client, wallet["id"], "10.50")

        assert response.status_code == 201
        body = response.json()
        assert body["kind"] == "deposit"
        assert body["replayed"] is False
        assert {entry["direction"] for entry in body["entries"]} == {"debit", "credit"}
        assert all(entry["amount"] == "10.50" for entry in body["entries"])
        assert balance_of(client, wallet["id"])["balance"]["amount"] == "10.50"

    def test_repeating_a_deposit_with_the_same_key_is_a_replay_not_a_second_deposit(self, client):
        wallet = open_wallet(client, "alice")
        headers = new_key()

        first = deposit(client, wallet["id"], "10.00", headers)
        second = deposit(client, wallet["id"], "10.00", headers)

        assert (first.status_code, second.status_code) == (201, 200)
        assert second.json()["replayed"] is True
        assert second.json()["id"] == first.json()["id"]
        assert balance_of(client, wallet["id"])["balance"]["amount"] == "10.00"

    def test_reusing_a_key_for_another_amount_is_a_conflict(self, client):
        wallet = open_wallet(client, "alice")
        headers = new_key()
        deposit(client, wallet["id"], "10.00", headers)

        response = deposit(client, wallet["id"], "11.00", headers)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "idempotency_conflict"

    def test_the_idempotency_key_is_required(self, client):
        wallet = open_wallet(client, "alice")

        response = client.post(f"/wallets/{wallet['id']}/deposits", json={"amount": "1.00"})

        assert response.status_code == 422

    @pytest.mark.parametrize("amount", ["abc", "0", "0.00", "-1", "1.234", "1e3", ""])
    def test_an_invalid_amount_is_a_bad_request(self, client, amount):
        wallet = open_wallet(client, "alice")

        response = deposit(client, wallet["id"], amount)

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"

    def test_a_withdrawal_reduces_the_balance(self, client):
        wallet = funded_wallet(client, "alice", "50.00")

        response = client.post(
            f"/wallets/{wallet['id']}/withdrawals", json={"amount": "20.25"}, headers=new_key()
        )

        assert response.status_code == 201
        assert balance_of(client, wallet["id"])["balance"]["amount"] == "29.75"

    def test_a_withdrawal_larger_than_the_balance_is_refused(self, client):
        wallet = funded_wallet(client, "alice", "10.00")

        response = client.post(
            f"/wallets/{wallet['id']}/withdrawals", json={"amount": "10.01"}, headers=new_key()
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "insufficient_funds"
        assert balance_of(client, wallet["id"])["balance"]["amount"] == "10.00"


class TestTransfers:
    def transfer(self, client, sender, receiver, amount, headers=None):
        return client.post(
            "/transfers",
            json={"from_wallet_id": sender["id"], "to_wallet_id": receiver["id"], "amount": amount},
            headers=headers or new_key(),
        )

    def test_moves_money_between_wallets(self, client):
        sender, receiver = funded_wallet(client, "alice", "100.00"), open_wallet(client, "bob")

        response = self.transfer(client, sender, receiver, "40.00")

        assert response.status_code == 201
        assert response.json()["kind"] == "transfer"
        assert balance_of(client, sender["id"])["balance"]["amount"] == "60.00"
        assert balance_of(client, receiver["id"])["balance"]["amount"] == "40.00"

    def test_a_transfer_to_oneself_is_a_bad_request(self, client):
        wallet = funded_wallet(client, "alice")

        response = self.transfer(client, wallet, wallet, "1.00")

        assert response.status_code == 400

    def test_a_transfer_between_currencies_is_refused(self, client):
        dollars, euros = funded_wallet(client, "alice"), open_wallet(client, "bob", "EUR")

        response = self.transfer(client, dollars, euros, "1.00")

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "currency_mismatch"

    def test_a_transfer_that_exceeds_the_balance_is_refused_and_changes_nothing(self, client):
        sender, receiver = funded_wallet(client, "alice", "5.00"), open_wallet(client, "bob")

        response = self.transfer(client, sender, receiver, "5.01")

        assert response.status_code == 422
        assert balance_of(client, sender["id"])["balance"]["amount"] == "5.00"
        assert balance_of(client, receiver["id"])["balance"]["amount"] == "0.00"

    def test_the_configured_fee_is_charged_to_the_sender(self, charging_client):
        client = charging_client
        sender, receiver = funded_wallet(client, "alice", "500.00"), open_wallet(client, "bob")

        response = self.transfer(client, sender, receiver, "100.00")

        assert response.status_code == 201
        assert len(response.json()["entries"]) == 3
        assert balance_of(client, sender["id"])["balance"]["amount"] == "399.00"
        assert balance_of(client, receiver["id"])["balance"]["amount"] == "100.00"
        fees = [
            r for r in client.get("/ledger/trial-balance").json()["rows"] if "fees" in r["code"]
        ]
        assert fees[0]["balance"] == "1.00"


class TestHolds:
    def place(self, client, wallet, amount, headers=None):
        return client.post(
            f"/wallets/{wallet['id']}/holds",
            json={"amount": amount, "reference": "order-1"},
            headers=headers or {},
        )

    def test_a_hold_reduces_the_available_balance_but_not_the_balance(self, client):
        wallet = funded_wallet(client, "alice", "100.00")

        response = self.place(client, wallet, "30.00")

        assert response.status_code == 201
        assert response.json()["status"] == "active"
        assert response.json()["amount"]["amount"] == "30.00"
        current = balance_of(client, wallet["id"])
        assert current["balance"]["amount"] == "100.00"
        assert current["held"]["amount"] == "30.00"
        assert current["available"]["amount"] == "70.00"

    def test_a_hold_larger_than_what_is_available_is_refused(self, client):
        wallet = funded_wallet(client, "alice", "10.00")

        response = self.place(client, wallet, "10.01")

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "insufficient_funds"

    def test_repeating_a_hold_with_the_same_key_is_a_replay(self, client):
        wallet = funded_wallet(client, "alice", "100.00")
        headers = new_key()

        first = self.place(client, wallet, "30.00", headers)
        second = self.place(client, wallet, "30.00", headers)

        assert (first.status_code, second.status_code) == (201, 200)
        assert second.json()["id"] == first.json()["id"]
        assert balance_of(client, wallet["id"])["held"]["amount"] == "30.00"

    def test_capturing_a_hold_pays_the_destination(self, client):
        payer, merchant = funded_wallet(client, "alice", "100.00"), open_wallet(client, "shop")
        hold = self.place(client, payer, "30.00").json()

        response = client.post(
            f"/holds/{hold['id']}/capture",
            json={"destination_wallet_id": merchant["id"]},
            headers=new_key(),
        )

        assert response.status_code == 201
        assert response.json()["kind"] == "hold_capture"
        assert balance_of(client, merchant["id"])["balance"]["amount"] == "30.00"
        assert balance_of(client, payer["id"])["held"]["amount"] == "0.00"
        assert client.get(f"/holds/{hold['id']}").json()["status"] == "captured"

    def test_a_partial_capture_uses_the_requested_amount(self, client):
        payer, merchant = funded_wallet(client, "alice", "100.00"), open_wallet(client, "shop")
        hold = self.place(client, payer, "30.00").json()

        client.post(
            f"/holds/{hold['id']}/capture",
            json={"destination_wallet_id": merchant["id"], "amount": "12.50"},
            headers=new_key(),
        )

        assert balance_of(client, merchant["id"])["balance"]["amount"] == "12.50"
        assert balance_of(client, payer["id"])["available"]["amount"] == "87.50"

    def test_a_hold_cannot_be_captured_twice(self, client):
        payer, merchant = funded_wallet(client, "alice", "100.00"), open_wallet(client, "shop")
        hold = self.place(client, payer, "30.00").json()
        body = {"destination_wallet_id": merchant["id"]}
        client.post(f"/holds/{hold['id']}/capture", json=body, headers=new_key())

        response = client.post(f"/holds/{hold['id']}/capture", json=body, headers=new_key())

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "hold_not_active"

    def test_releasing_a_hold_frees_the_money(self, client):
        wallet = funded_wallet(client, "alice", "100.00")
        hold = self.place(client, wallet, "30.00").json()

        response = client.post(f"/holds/{hold['id']}/release")

        assert response.status_code == 200
        assert response.json()["status"] == "released"
        assert balance_of(client, wallet["id"])["available"]["amount"] == "100.00"

    def test_an_unknown_hold_is_not_found(self, client):
        assert client.get(f"/holds/{uuid4()}").status_code == 404
        assert client.post(f"/holds/{uuid4()}/release").json()["error"]["code"] == "hold_not_found"


class TestReversalsAndReading:
    def test_reversing_a_deposit_restores_the_balance(self, client):
        wallet = open_wallet(client, "alice")
        posted = deposit(client, wallet["id"], "25.00").json()

        response = client.post(
            f"/transactions/{posted['id']}/reversal",
            json={"reason": "Duplicate"},
            headers=new_key(),
        )

        assert response.status_code == 201
        assert response.json()["kind"] == "reversal"
        assert response.json()["reverses_transaction_id"] == posted["id"]
        assert balance_of(client, wallet["id"])["balance"]["amount"] == "0.00"

    def test_a_transaction_cannot_be_reversed_twice(self, client):
        wallet = open_wallet(client, "alice")
        posted = deposit(client, wallet["id"], "25.00").json()
        path = f"/transactions/{posted['id']}/reversal"
        client.post(path, json={"reason": "First"}, headers=new_key())

        response = client.post(path, json={"reason": "Second"}, headers=new_key())

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "already_reversed"

    def test_a_reversal_needs_a_reason(self, client):
        wallet = open_wallet(client, "alice")
        posted = deposit(client, wallet["id"], "25.00").json()

        assert client.post(f"/transactions/{posted['id']}/reversal", json={}).status_code == 422

    def test_a_posted_transaction_can_be_read_back(self, client):
        wallet = open_wallet(client, "alice")
        posted = deposit(client, wallet["id"], "25.00").json()

        stored = client.get(f"/transactions/{posted['id']}").json()

        assert stored["id"] == posted["id"]
        assert stored["entries"] == posted["entries"]

    def test_an_unknown_transaction_is_not_found(self, client):
        response = client.get(f"/transactions/{uuid4()}")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "transaction_not_found"


class TestStatement:
    def test_pages_through_the_history_newest_first(self, client):
        wallet = open_wallet(client, "alice")
        for amount in ["1.00", "2.00", "3.00", "4.00", "5.00"]:
            deposit(client, wallet["id"], amount)

        first = client.get(f"/wallets/{wallet['id']}/statement", params={"limit": 2}).json()
        second = client.get(
            f"/wallets/{wallet['id']}/statement",
            params={"limit": 2, "before_seq": first["next_before_seq"]},
        ).json()
        last = client.get(
            f"/wallets/{wallet['id']}/statement",
            params={"limit": 2, "before_seq": second["next_before_seq"]},
        ).json()

        assert [line["amount"] for line in first["lines"]] == ["5.00", "4.00"]
        assert [line["amount"] for line in second["lines"]] == ["3.00", "2.00"]
        assert [line["amount"] for line in last["lines"]] == ["1.00"]
        assert last["next_before_seq"] is None
        assert first["lines"][0]["balance_after"] == "15.00"

    def test_signs_each_line_by_its_effect(self, client):
        wallet = funded_wallet(client, "alice", "10.00")
        client.post(
            f"/wallets/{wallet['id']}/withdrawals", json={"amount": "4.00"}, headers=new_key()
        )

        lines = client.get(f"/wallets/{wallet['id']}/statement").json()["lines"]

        assert [(line["kind"], line["signed_amount"]) for line in lines] == [
            ("withdrawal", "-4.00"),
            ("deposit", "10.00"),
        ]

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"before_seq": 0}])
    def test_rejects_out_of_range_paging(self, client, params):
        wallet = open_wallet(client, "alice")

        assert client.get(f"/wallets/{wallet['id']}/statement", params=params).status_code == 422


class TestReports:
    def test_the_trial_balance_shows_debits_equal_credits_per_currency(self, client):
        alice = funded_wallet(client, "alice", "100.00")
        bob = funded_wallet(client, "bob", "50.00", "EUR")
        client.post(
            f"/wallets/{alice['id']}/withdrawals", json={"amount": "30.00"}, headers=new_key()
        )

        report = client.get("/ledger/trial-balance").json()

        assert {total["currency"]: total["balanced"] for total in report["totals"]} == {
            "EUR": True,
            "USD": True,
        }
        usd_wallet = next(row for row in report["rows"] if row["code"] == "wallet:alice:USD")
        assert (usd_wallet["debits"], usd_wallet["credits"], usd_wallet["balance"]) == (
            "30.00",
            "100.00",
            "70.00",
        )
        assert bob["currency"] == "EUR"

    def test_the_integrity_report_is_clean_after_normal_use(self, client):
        wallet = funded_wallet(client, "alice")
        client.post(f"/wallets/{wallet['id']}/holds", json={"amount": "10.00"})

        assert client.get("/ledger/integrity").json() == {"ok": True, "issues": []}


def test_the_openapi_document_lists_the_money_moving_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]

    assert {"/transfers", "/wallets/{wallet_id}/deposits", "/holds/{hold_id}/capture"} <= set(paths)
