-- Holds reserve part of a balance without moving it, the way a card authorization does.
-- The invariant balance >= held >= 0 is enforced here, under the same row lock as postings.

CREATE TYPE hold_status AS ENUM ('active', 'captured', 'released', 'expired');

CREATE TABLE holds (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id uuid NOT NULL REFERENCES accounts (id),
    amount_minor bigint NOT NULL CHECK (amount_minor > 0),
    status hold_status NOT NULL DEFAULT 'active',
    reference text,
    idempotency_key text UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    request_fingerprint text,
    expires_at timestamptz,
    -- Deferred: a capture resolves the hold first (freeing the held amount) and then posts
    -- the debit, so the transaction row does not exist yet when the hold is updated.
    captured_transaction_id uuid REFERENCES transactions (id) DEFERRABLE INITIALLY DEFERRED,
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    CHECK ((status = 'active') = (resolved_at IS NULL)),
    CHECK ((status = 'captured') = (captured_transaction_id IS NOT NULL)),
    CHECK ((idempotency_key IS NULL) = (request_fingerprint IS NULL))
);

CREATE INDEX holds_active_idx ON holds (account_id) WHERE status = 'active';
CREATE INDEX holds_due_idx ON holds (expires_at) WHERE status = 'active' AND expires_at IS NOT NULL;

CREATE FUNCTION holds_reserve() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target accounts%ROWTYPE;
    updated account_balances%ROWTYPE;
BEGIN
    IF NEW.status <> 'active' THEN
        RAISE EXCEPTION 'a hold must start active' USING ERRCODE = 'LD009';
    END IF;

    SELECT * INTO target FROM accounts WHERE id = NEW.account_id;
    IF target.is_closed THEN
        RAISE EXCEPTION 'account % is closed', target.code USING ERRCODE = 'LD003';
    END IF;

    UPDATE account_balances
       SET held_minor = held_minor + NEW.amount_minor, updated_at = now()
     WHERE account_id = NEW.account_id
    RETURNING * INTO updated;

    IF NOT target.allow_negative AND updated.balance_minor - updated.held_minor < 0 THEN
        RAISE EXCEPTION 'insufficient funds on account % to hold this amount', target.code
            USING ERRCODE = 'LD001';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER holds_reserve BEFORE INSERT ON holds
    FOR EACH ROW EXECUTE FUNCTION holds_reserve();

CREATE FUNCTION holds_resolve() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'holds cannot be deleted' USING ERRCODE = 'LD006';
    END IF;

    IF OLD.status <> 'active' THEN
        RAISE EXCEPTION 'hold % is already %', OLD.id, OLD.status USING ERRCODE = 'LD009';
    END IF;

    IF NEW.account_id <> OLD.account_id OR NEW.amount_minor <> OLD.amount_minor THEN
        RAISE EXCEPTION 'the account and amount of a hold cannot change' USING ERRCODE = 'LD007';
    END IF;

    IF NEW.status <> 'active' THEN
        UPDATE account_balances
           SET held_minor = held_minor - OLD.amount_minor, updated_at = now()
         WHERE account_id = OLD.account_id;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER holds_resolve BEFORE UPDATE OR DELETE ON holds
    FOR EACH ROW EXECUTE FUNCTION holds_resolve();
