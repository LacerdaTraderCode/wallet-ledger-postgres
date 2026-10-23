-- Double-entry ledger core. Every rule that protects money lives in the database,
-- so a bug in the application (or a second application) cannot break it.

CREATE TYPE account_type AS ENUM ('asset', 'liability', 'equity', 'revenue', 'expense');
CREATE TYPE entry_direction AS ENUM ('debit', 'credit');

-- The side that increases an account: debit for assets and expenses, credit for the rest.
CREATE FUNCTION normal_direction(kind account_type) RETURNS entry_direction
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN kind IN ('asset', 'expense') THEN 'debit'::entry_direction
                ELSE 'credit'::entry_direction END
$$;

CREATE FUNCTION reject_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% on % is not allowed: the ledger is append-only, post a reversal instead',
        TG_OP, TG_TABLE_NAME USING ERRCODE = 'LD006';
END
$$;

CREATE TABLE accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL UNIQUE CHECK (length(code) BETWEEN 1 AND 120),
    name text NOT NULL CHECK (length(name) > 0),
    type account_type NOT NULL,
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    allow_negative boolean NOT NULL DEFAULT false,
    owner_ref text,
    is_closed boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, currency)
);

CREATE TABLE account_balances (
    account_id uuid PRIMARY KEY REFERENCES accounts (id),
    balance_minor bigint NOT NULL DEFAULT 0,
    held_minor bigint NOT NULL DEFAULT 0 CHECK (held_minor >= 0),
    entry_count bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION accounts_open_balance() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO account_balances (account_id) VALUES (NEW.id);
    RETURN NEW;
END
$$;

CREATE TRIGGER accounts_open_balance AFTER INSERT ON accounts
    FOR EACH ROW EXECUTE FUNCTION accounts_open_balance();

CREATE FUNCTION accounts_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    current_balance account_balances%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'accounts cannot be deleted, close them instead'
            USING ERRCODE = 'LD006';
    END IF;

    IF NEW.id <> OLD.id OR NEW.code <> OLD.code OR NEW.type <> OLD.type
        OR NEW.currency <> OLD.currency OR NEW.allow_negative <> OLD.allow_negative THEN
        RAISE EXCEPTION 'code, type, currency and allow_negative of account % cannot change', OLD.code
            USING ERRCODE = 'LD007';
    END IF;

    IF OLD.is_closed AND NOT NEW.is_closed THEN
        RAISE EXCEPTION 'account % is closed and cannot be reopened', OLD.code
            USING ERRCODE = 'LD003';
    END IF;

    IF NEW.is_closed AND NOT OLD.is_closed THEN
        SELECT * INTO current_balance FROM account_balances WHERE account_id = OLD.id FOR UPDATE;
        IF current_balance.balance_minor <> 0 OR current_balance.held_minor <> 0 THEN
            RAISE EXCEPTION 'account % still has a balance or holds and cannot be closed', OLD.code
                USING ERRCODE = 'LD008';
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER accounts_guard BEFORE UPDATE OR DELETE ON accounts
    FOR EACH ROW EXECUTE FUNCTION accounts_guard();

CREATE TABLE transactions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL
        CHECK (kind IN ('deposit', 'withdrawal', 'transfer', 'hold_capture', 'reversal', 'adjustment')),
    description text NOT NULL CHECK (length(description) > 0),
    idempotency_key text UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    request_fingerprint text,
    reverses_transaction_id uuid UNIQUE REFERENCES transactions (id),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- Lets the entries trigger prove a row is added in the transaction that created its parent.
    created_xact xid8 NOT NULL DEFAULT pg_current_xact_id(),
    CHECK ((kind = 'reversal') = (reverses_transaction_id IS NOT NULL)),
    CHECK ((idempotency_key IS NULL) = (request_fingerprint IS NULL))
);

CREATE TABLE entries (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id uuid NOT NULL REFERENCES transactions (id),
    account_id uuid NOT NULL,
    currency char(3) NOT NULL,
    direction entry_direction NOT NULL,
    amount_minor bigint NOT NULL CHECK (amount_minor > 0),
    -- Both are set by the trigger below, under the account's balance lock.
    account_seq bigint NOT NULL,
    balance_after_minor bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- An entry must be in the currency of its account.
    FOREIGN KEY (account_id, currency) REFERENCES accounts (id, currency),
    UNIQUE (account_id, account_seq)
);

CREATE INDEX entries_transaction_idx ON entries (transaction_id);

-- Runs before each entry is stored. Updating the balance row takes a row lock, which
-- serializes every posting to one account; the sequence it hands out is therefore a
-- total order per account that matches commit order.
CREATE FUNCTION entries_apply_to_balance() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target accounts%ROWTYPE;
    change bigint;
    updated account_balances%ROWTYPE;
BEGIN
    -- The CHECK on amount_minor would also refuse this, but only after this trigger had
    -- already judged the entry against the balance; checking first keeps the error stable.
    IF NEW.amount_minor <= 0 THEN
        RAISE EXCEPTION 'entry amounts must be positive' USING ERRCODE = '23514';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM transactions t
        WHERE t.id = NEW.transaction_id AND t.created_xact = pg_current_xact_id()
    ) THEN
        RAISE EXCEPTION 'entries can only be added in the transaction that created their parent'
            USING ERRCODE = 'LD004';
    END IF;

    SELECT * INTO target FROM accounts WHERE id = NEW.account_id;
    IF target.is_closed THEN
        RAISE EXCEPTION 'account % is closed', target.code USING ERRCODE = 'LD003';
    END IF;

    change := CASE WHEN NEW.direction = normal_direction(target.type)
                   THEN NEW.amount_minor ELSE -NEW.amount_minor END;

    UPDATE account_balances
       SET balance_minor = balance_minor + change,
           entry_count = entry_count + 1,
           updated_at = now()
     WHERE account_id = NEW.account_id
    RETURNING * INTO updated;

    IF NOT target.allow_negative AND updated.balance_minor - updated.held_minor < 0 THEN
        RAISE EXCEPTION 'insufficient funds on account %', target.code USING ERRCODE = 'LD001';
    END IF;

    NEW.account_seq := updated.entry_count;
    NEW.balance_after_minor := updated.balance_minor;
    RETURN NEW;
END
$$;

CREATE TRIGGER entries_apply_to_balance BEFORE INSERT ON entries
    FOR EACH ROW EXECUTE FUNCTION entries_apply_to_balance();

CREATE FUNCTION assert_transaction_balanced() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    unbalanced record;
BEGIN
    SELECT e.currency AS currency,
           sum(CASE e.direction WHEN 'debit' THEN e.amount_minor ELSE -e.amount_minor END) AS difference
      INTO unbalanced
      FROM entries e
     WHERE e.transaction_id = NEW.transaction_id
     GROUP BY e.currency
    HAVING sum(CASE e.direction WHEN 'debit' THEN e.amount_minor ELSE -e.amount_minor END) <> 0
     LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION 'transaction % is not balanced in %: debits minus credits is %',
            NEW.transaction_id, unbalanced.currency, unbalanced.difference
            USING ERRCODE = 'LD002';
    END IF;
    RETURN NULL;
END
$$;

-- Deferred, so all entries of a transaction can be inserted one by one before the check runs.
CREATE CONSTRAINT TRIGGER entries_must_balance AFTER INSERT ON entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_transaction_balanced();

CREATE FUNCTION assert_transaction_has_entries() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT count(*) FROM entries WHERE transaction_id = NEW.id) < 2 THEN
        RAISE EXCEPTION 'transaction % needs at least two entries', NEW.id USING ERRCODE = 'LD005';
    END IF;
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER transactions_need_entries AFTER INSERT ON transactions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_transaction_has_entries();

CREATE TRIGGER transactions_append_only BEFORE UPDATE OR DELETE ON transactions
    FOR EACH ROW EXECUTE FUNCTION reject_change();
CREATE TRIGGER entries_append_only BEFORE UPDATE OR DELETE ON entries
    FOR EACH ROW EXECUTE FUNCTION reject_change();
CREATE TRIGGER transactions_no_truncate BEFORE TRUNCATE ON transactions
    FOR EACH STATEMENT EXECUTE FUNCTION reject_change();
CREATE TRIGGER entries_no_truncate BEFORE TRUNCATE ON entries
    FOR EACH STATEMENT EXECUTE FUNCTION reject_change();
