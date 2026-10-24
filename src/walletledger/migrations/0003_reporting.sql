CREATE VIEW trial_balance AS
SELECT a.id AS account_id,
       a.code,
       a.name,
       a.type,
       a.currency,
       coalesce(sum(e.amount_minor) FILTER (WHERE e.direction = 'debit'), 0)::bigint AS debits_minor,
       coalesce(sum(e.amount_minor) FILTER (WHERE e.direction = 'credit'), 0)::bigint AS credits_minor,
       b.balance_minor,
       b.held_minor
  FROM accounts a
  JOIN account_balances b ON b.account_id = a.id
  LEFT JOIN entries e ON e.account_id = a.id
 GROUP BY a.id, b.account_id;

-- Recomputes everything the triggers maintain and reports each disagreement.
-- An empty result means the ledger is consistent.
CREATE FUNCTION ledger_integrity_report()
RETURNS TABLE (check_name text, subject text, expected bigint, actual bigint)
LANGUAGE sql STABLE AS $$
    SELECT 'transaction_balanced', e.transaction_id::text || ' ' || e.currency, 0::bigint,
           sum(CASE e.direction WHEN 'debit' THEN e.amount_minor ELSE -e.amount_minor END)::bigint
      FROM entries e
     GROUP BY e.transaction_id, e.currency
    HAVING sum(CASE e.direction WHEN 'debit' THEN e.amount_minor ELSE -e.amount_minor END) <> 0

    UNION ALL
    SELECT 'currency_balanced', e.currency::text, 0::bigint,
           sum(CASE e.direction WHEN 'debit' THEN e.amount_minor ELSE -e.amount_minor END)::bigint
      FROM entries e
     GROUP BY e.currency
    HAVING sum(CASE e.direction WHEN 'debit' THEN e.amount_minor ELSE -e.amount_minor END) <> 0

    UNION ALL
    SELECT 'stored_balance', a.code,
           coalesce(sum(CASE WHEN e.direction = normal_direction(a.type)
                             THEN e.amount_minor ELSE -e.amount_minor END), 0)::bigint,
           b.balance_minor
      FROM accounts a
      JOIN account_balances b ON b.account_id = a.id
      LEFT JOIN entries e ON e.account_id = a.id
     GROUP BY a.id, b.account_id
    HAVING coalesce(sum(CASE WHEN e.direction = normal_direction(a.type)
                             THEN e.amount_minor ELSE -e.amount_minor END), 0) <> b.balance_minor

    UNION ALL
    SELECT 'stored_entry_count', a.code, count(e.id)::bigint, b.entry_count
      FROM accounts a
      JOIN account_balances b ON b.account_id = a.id
      LEFT JOIN entries e ON e.account_id = a.id
     GROUP BY a.id, b.account_id
    HAVING count(e.id) <> b.entry_count

    UNION ALL
    SELECT 'stored_held', a.code,
           coalesce(sum(h.amount_minor) FILTER (WHERE h.status = 'active'), 0)::bigint,
           b.held_minor
      FROM accounts a
      JOIN account_balances b ON b.account_id = a.id
      LEFT JOIN holds h ON h.account_id = a.id
     GROUP BY a.id, b.account_id
    HAVING coalesce(sum(h.amount_minor) FILTER (WHERE h.status = 'active'), 0) <> b.held_minor

    UNION ALL
    SELECT 'running_balance', chain.account_id::text || '#' || chain.account_seq,
           chain.running, chain.balance_after_minor
      FROM (
          SELECT e.account_id, e.account_seq, e.balance_after_minor,
                 sum(CASE WHEN e.direction = normal_direction(a.type)
                          THEN e.amount_minor ELSE -e.amount_minor END)
                     OVER (PARTITION BY e.account_id ORDER BY e.account_seq)::bigint AS running
            FROM entries e
            JOIN accounts a ON a.id = e.account_id
      ) chain
     WHERE chain.running <> chain.balance_after_minor
$$;
