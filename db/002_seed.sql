-- =============================================================================
-- POC seed data: reference values, dimension catalog and example rules
-- (including R101 / R102 from spec §7).
-- =============================================================================

SET search_path = billing_config, public;

INSERT INTO ref_merchant (merchant_id, display_name) VALUES
    ('M123', 'Acme Apparel'),
    ('M456', 'Northwind Outdoor'),
    ('M789', 'Lumen Beauty');

INSERT INTO ref_fee_type (fee_type, display_name) VALUES
    ('platform_fee',  'Platform fee'),
    ('shipping_fee',  'Shipping fee'),
    ('deferment_fee', 'Deferment fee'),
    ('duties',        'Duties'),
    ('taxes',         'Taxes');

INSERT INTO ref_partner (partner_id, display_name) VALUES
    ('PTR001', 'Border Logistics Partner'),
    ('PTR002', 'Northern Brokerage Partner');

INSERT INTO billing_dimension_catalog
    (dimension_key, display_name, value_source, is_available_in_staging, sort_order, description) VALUES
    ('DESTINATION_COUNTRY', 'Destination Country', 'Country reference',    TRUE, 10, 'ISO country the order ships to'),
    ('ORIGIN_COUNTRY',      'Origin Country',      'Country reference',    TRUE, 20, 'ISO country of origin'),
    ('CARRIER',             'Carrier',             'OMS carrier reference', TRUE, 30, 'Canonical OMS carrier identifier used for matching (not the payer)'),
    ('FC_CODE',             'Fulfillment Center',  'OMS FC reference',     TRUE, 40, 'Fulfillment center identifier'),
    -- Registered but not yet staged in Snowflake: demonstrates spec §11.4 and
    -- the "dimension is not yet available in billing staging" validation.
    ('SERVICE_LEVEL',       'Service Level',       'OMS service-level reference', FALSE, 50, 'Canonical shipping service level');

INSERT INTO ref_dimension_value (dimension_key, dimension_value, display_name)
SELECT d.k, v.code, v.name
FROM (VALUES ('DESTINATION_COUNTRY'), ('ORIGIN_COUNTRY')) d(k)
CROSS JOIN (VALUES
    ('US', 'United States'), ('CA', 'Canada'), ('MX', 'Mexico'), ('GB', 'United Kingdom'),
    ('DE', 'Germany'), ('FR', 'France'), ('AU', 'Australia'), ('JP', 'Japan'),
    ('CN', 'China'), ('VN', 'Vietnam'), ('HK', 'Hong Kong'), ('KR', 'South Korea'),
    ('IN', 'India'), ('BR', 'Brazil')
) v(code, name);

INSERT INTO ref_dimension_value (dimension_key, dimension_value, display_name) VALUES
    ('CARRIER', 'DHL_EXPRESS', 'DHL Express'),
    ('CARRIER', 'FEDEX',       'FedEx'),
    ('CARRIER', 'UPS',         'UPS'),
    ('CARRIER', 'USPS',        'USPS'),
    ('CARRIER', 'CANADA_POST', 'Canada Post'),
    ('CARRIER', 'ROYAL_MAIL',  'Royal Mail'),
    ('CARRIER', 'ARAMEX',      'Aramex'),
    ('FC_CODE', 'HKG01', 'Hong Kong 01'),
    ('FC_CODE', 'SZX01', 'Shenzhen 01'),
    ('FC_CODE', 'LAX01', 'Los Angeles 01'),
    ('FC_CODE', 'YYZ01', 'Toronto 01'),
    ('FC_CODE', 'AMS01', 'Amsterdam 01'),
    ('SERVICE_LEVEL', 'EXPRESS',  'Express'),
    ('SERVICE_LEVEL', 'STANDARD', 'Standard'),
    ('SERVICE_LEVEL', 'ECONOMY',  'Economy');

-- -----------------------------------------------------------------------------
-- Seed helper: creates a rule the same way the API does (draft -> conditions ->
-- publish) so every integrity trigger is exercised, and writes audit events.
-- -----------------------------------------------------------------------------
CREATE FUNCTION pg_temp.seed_rule(
    p_logical      UUID,
    p_revision     INT,
    p_merchant     TEXT,
    p_fee          TEXT,
    p_payer_type   TEXT,
    p_payer_ref    TEXT,
    p_waived       BOOLEAN,
    p_from         TIMESTAMPTZ,
    p_to           TIMESTAMPTZ,
    p_status       TEXT,          -- DRAFT | PUBLISHED | RETIRED
    p_conditions   JSONB,         -- {"DIM": ["V1","V2"], ...}
    p_reason       TEXT
) RETURNS UUID LANGUAGE plpgsql AS $$
DECLARE
    v_rule UUID;
    v_dim  TEXT;
    v_vals JSONB;
BEGIN
    INSERT INTO billing_config.billing_rule
        (logical_rule_id, revision, merchant_id, fee_type, payer_type, payer_reference,
         is_waived, effective_from, effective_to, change_reason, created_by, created_at)
    VALUES (p_logical, p_revision, p_merchant, p_fee, p_payer_type, p_payer_ref,
            p_waived, p_from, p_to, p_reason, 'seed@openborder.com', p_from - interval '7 days')
    RETURNING rule_id INTO v_rule;

    FOR v_dim, v_vals IN SELECT * FROM jsonb_each(coalesce(p_conditions, '{}'::jsonb)) LOOP
        INSERT INTO billing_config.billing_rule_condition (rule_id, dimension_key, dimension_value)
        SELECT v_rule, v_dim, jsonb_array_elements_text(v_vals);
    END LOOP;

    INSERT INTO billing_config.billing_rule_audit (rule_id, logical_rule_id, action, actor, reason)
    VALUES (v_rule, p_logical, 'CREATED', 'seed@openborder.com', p_reason);

    IF p_status IN ('PUBLISHED', 'RETIRED') THEN
        UPDATE billing_config.billing_rule
           SET status = 'PUBLISHED', published_at = p_from - interval '3 days', published_by = 'seed@openborder.com'
         WHERE rule_id = v_rule;
        INSERT INTO billing_config.billing_rule_audit (rule_id, logical_rule_id, action, actor, reason)
        VALUES (v_rule, p_logical, 'PUBLISHED', 'seed@openborder.com', p_reason);
    END IF;

    IF p_status = 'RETIRED' THEN
        UPDATE billing_config.billing_rule
           SET status = 'RETIRED', retired_at = p_to - interval '10 days', retired_by = 'seed@openborder.com'
         WHERE rule_id = v_rule;
        INSERT INTO billing_config.billing_rule_audit (rule_id, logical_rule_id, action, actor, reason)
        VALUES (v_rule, p_logical, 'RETIRED', 'seed@openborder.com', 'Carrier contract ended');
    END IF;

    RETURN v_rule;
END;
$$;

-- M123 duties: catch-all default (no conditions)
SELECT pg_temp.seed_rule('c0de0100-0000-4000-8000-000000000000', 1, 'M123', 'duties', 'MERCHANT', NULL,
    FALSE, '2026-01-01T00:00:00Z', NULL, 'PUBLISHED', '{}',
    'Default: merchant pays duties');

-- Spec §7 R101
SELECT pg_temp.seed_rule('c0de0101-0000-4000-8000-000000000000', 1, 'M123', 'duties', 'MERCHANT', NULL,
    TRUE, '2026-11-01T00:00:00Z', NULL, 'PUBLISHED',
    '{"DESTINATION_COUNTRY": ["CA","US","MX"], "CARRIER": ["DHL_EXPRESS","FEDEX"]}',
    'North America duty waiver promotion (R101)');

-- Spec §7 R102
SELECT pg_temp.seed_rule('c0de0102-0000-4000-8000-000000000000', 1, 'M123', 'duties', 'MERCHANT', NULL,
    FALSE, '2026-11-01T00:00:00Z', NULL, 'PUBLISHED',
    '{"DESTINATION_COUNTRY": ["CA"], "CARRIER": ["DHL_EXPRESS"], "ORIGIN_COUNTRY": ["CN"], "FC_CODE": ["HKG01"]}',
    'Exclude CN-origin HKG01 DHL shipments to Canada from waiver (R102)');

-- M123 shipping_fee: two revisions (rev 1 superseded by rev 2 on 2026-07-01)
SELECT pg_temp.seed_rule('c0de0103-0000-4000-8000-000000000000', 1, 'M123', 'shipping_fee', 'MERCHANT', NULL,
    TRUE, '2026-01-01T00:00:00Z', '2026-07-01T00:00:00Z', 'PUBLISHED', '{}',
    'H1 launch promotion: shipping waived');
INSERT INTO billing_rule_audit (rule_id, logical_rule_id, action, actor, reason)
SELECT rule_id, logical_rule_id, 'SUPERSEDED', 'seed@openborder.com', 'Superseded by revision 2'
FROM billing_rule WHERE logical_rule_id = 'c0de0103-0000-4000-8000-000000000000' AND revision = 1;
SELECT pg_temp.seed_rule('c0de0103-0000-4000-8000-000000000000', 2, 'M123', 'shipping_fee', 'MERCHANT', NULL,
    FALSE, '2026-07-01T00:00:00Z', NULL, 'PUBLISHED', '{}',
    'Promotion ended: merchant pays shipping');

-- M456 platform_fee: partner pays for EU/UK destinations, waived
SELECT pg_temp.seed_rule('c0de0104-0000-4000-8000-000000000000', 1, 'M456', 'platform_fee', 'PARTNER', 'PTR001',
    TRUE, '2026-03-01T00:00:00Z', NULL, 'PUBLISHED',
    '{"DESTINATION_COUNTRY": ["GB","DE","FR"]}',
    'Partner-sponsored EU/UK launch');

-- M456 platform_fee default
SELECT pg_temp.seed_rule('c0de0105-0000-4000-8000-000000000000', 1, 'M456', 'platform_fee', 'MERCHANT', NULL,
    FALSE, '2026-03-01T00:00:00Z', NULL, 'PUBLISHED', '{}',
    'Default: merchant pays platform fee');

-- M456 shipping_fee: carrier-paid arrangement, retired 2026-09-01
SELECT pg_temp.seed_rule('c0de0106-0000-4000-8000-000000000000', 1, 'M456', 'shipping_fee', 'CARRIER', NULL,
    FALSE, '2026-02-01T00:00:00Z', '2026-09-01T00:00:00Z', 'RETIRED',
    '{"CARRIER": ["UPS"], "FC_CODE": ["LAX01","YYZ01"]}',
    'UPS carrier-paid shipping from LAX01/YYZ01');

-- M789 deferment_fee: incomplete draft (partner payer with no partner chosen)
SELECT pg_temp.seed_rule('c0de0107-0000-4000-8000-000000000000', 1, 'M789', 'deferment_fee', 'PARTNER', NULL,
    FALSE, '2026-11-15T00:00:00Z', NULL, 'DRAFT',
    '{"ORIGIN_COUNTRY": ["CN","VN"]}',
    'WIP: partner to absorb deferment fees on Asia-origin orders');
