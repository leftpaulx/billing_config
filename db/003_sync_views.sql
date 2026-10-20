-- =============================================================================
-- Read path for the Data-owned PostgreSQL -> Snowflake sync (spec §5.2, §10)
--
-- The Prefect flow must read both views inside ONE snapshot transaction so that
-- rules and conditions are consistent:
--
--   BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
--   SELECT * FROM billing_config.v_sync_billing_rule;
--   SELECT * FROM billing_config.v_sync_billing_rule_condition;
--   COMMIT;
--
-- RETIRED revisions are included while they have a non-empty effective window,
-- because late billing events can still fall inside their historical window
-- (BR-06: match on billing_event_at, not processing time). Drafts and DELETED
-- rules never sync.
-- =============================================================================

SET search_path = billing_config, public;

CREATE VIEW v_sync_billing_rule AS
SELECT rule_id,
       logical_rule_id,
       revision,
       merchant_id,
       fee_type,
       payer_type,
       payer_reference,
       is_waived,
       effective_from,
       effective_to,
       published_at
FROM billing_rule
WHERE status IN ('PUBLISHED', 'RETIRED')
  AND (effective_to IS NULL OR effective_to > effective_from);

CREATE VIEW v_sync_billing_rule_condition AS
SELECT c.rule_id,
       c.dimension_key,
       c.dimension_value
FROM billing_rule_condition c
JOIN v_sync_billing_rule r ON r.rule_id = c.rule_id;

-- Read-only role for the sync (spec §14). NOLOGIN group role; grant it to the
-- actual Prefect login user. Skipped silently where role creation is not allowed.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'billing_sync_ro') THEN
        CREATE ROLE billing_sync_ro NOLOGIN;
    END IF;
    GRANT USAGE ON SCHEMA billing_config TO billing_sync_ro;
    GRANT SELECT ON billing_config.v_sync_billing_rule,
                    billing_config.v_sync_billing_rule_condition,
                    billing_config.billing_dimension_catalog TO billing_sync_ro;
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'Skipping billing_sync_ro role setup: insufficient privilege';
END;
$$;
