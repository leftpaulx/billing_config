-- =============================================================================
-- OpenBorder Billing Configuration — PostgreSQL schema (POC)
-- Implements tech spec §6 (data model), §6.1/§6.2 constraints, §6.4 audit,
-- §13 versioning rules, and §15 indexes.
--
-- Requires PostgreSQL 13+ (gen_random_uuid() is built in). No extensions needed:
-- the "no overlapping published revisions" rule is enforced by a constraint
-- trigger rather than a btree_gist exclusion constraint, so this runs on any
-- managed Postgres.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS billing_config;
SET search_path = billing_config, public;

-- -----------------------------------------------------------------------------
-- Reference data
-- In production these are owned by OMS / reference-data services (spec §18 #9).
-- For the POC they are local tables so the API can validate identifiers.
-- -----------------------------------------------------------------------------

CREATE TABLE ref_merchant (
    merchant_id   VARCHAR(64)  PRIMARY KEY,
    display_name  VARCHAR(200) NOT NULL,
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE
);

-- BR-01: new fee types are added here, never in matching logic.
CREATE TABLE ref_fee_type (
    fee_type      VARCHAR(64)  PRIMARY KEY CHECK (fee_type ~ '^[a-z][a-z0-9_]*$'),
    display_name  VARCHAR(200) NOT NULL,
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE
);

CREATE TABLE ref_partner (
    partner_id    VARCHAR(64)  PRIMARY KEY,
    display_name  VARCHAR(200) NOT NULL,
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE
);

-- §6.3 Controlled registry of supported matching dimensions.
CREATE TABLE billing_dimension_catalog (
    dimension_key            VARCHAR(64)  PRIMARY KEY CHECK (dimension_key ~ '^[A-Z][A-Z0-9_]*$'),
    display_name             VARCHAR(200) NOT NULL,
    data_type                VARCHAR(16)  NOT NULL DEFAULT 'STRING' CHECK (data_type IN ('STRING')),
    value_source             VARCHAR(200) NOT NULL,
    is_active                BOOLEAN      NOT NULL DEFAULT TRUE,
    -- §8.4 / §9.3 #9: set TRUE by Data once the attribute is present in
    -- Snowflake match_context. Rules using a dimension that is not yet staged
    -- cannot be published.
    is_available_in_staging  BOOLEAN      NOT NULL DEFAULT FALSE,
    description              TEXT,
    sort_order               INTEGER      NOT NULL DEFAULT 100,
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Canonical selectable values per dimension (POC stand-in for the
-- "Country reference" / "OMS carrier reference" / "OMS FC reference" sources).
CREATE TABLE ref_dimension_value (
    dimension_key    VARCHAR(64)  NOT NULL REFERENCES billing_dimension_catalog (dimension_key),
    dimension_value  VARCHAR(128) NOT NULL CHECK (dimension_value ~ '^[A-Z0-9][A-Z0-9_\-]*$'),
    display_name     VARCHAR(200) NOT NULL,
    is_active        BOOLEAN      NOT NULL DEFAULT TRUE,
    PRIMARY KEY (dimension_key, dimension_value)
);

-- -----------------------------------------------------------------------------
-- §6.1 billing_rule — one row per rule *version*
-- -----------------------------------------------------------------------------

CREATE TABLE billing_rule (
    rule_id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    logical_rule_id  UUID         NOT NULL DEFAULT gen_random_uuid(),
    revision         INTEGER      NOT NULL DEFAULT 1 CHECK (revision >= 1),
    merchant_id      VARCHAR(64)  NOT NULL REFERENCES ref_merchant (merchant_id),
    fee_type         VARCHAR(64)  NOT NULL REFERENCES ref_fee_type (fee_type),
    -- Nullable only while DRAFT ("Save Draft persists incomplete configuration").
    payer_type       VARCHAR(16)  CHECK (payer_type IN ('MERCHANT', 'CARRIER', 'PARTNER')),
    payer_reference  VARCHAR(128),
    is_waived        BOOLEAN      NOT NULL DEFAULT FALSE,
    effective_from   TIMESTAMPTZ,                       -- inclusive
    effective_to     TIMESTAMPTZ,                       -- exclusive, NULL = open-ended
    status           VARCHAR(16)  NOT NULL DEFAULT 'DRAFT'
                                  CHECK (status IN ('DRAFT', 'PUBLISHED', 'RETIRED', 'DELETED')),
    change_reason    TEXT,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_by       VARCHAR(200) NOT NULL,
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_by       VARCHAR(200),
    published_at     TIMESTAMPTZ,
    published_by     VARCHAR(200),
    retired_at       TIMESTAMPTZ,
    retired_by       VARCHAR(200),
    -- Admin delete is a soft delete: the row and its audit trail are kept because
    -- Snowflake snapshots and billing ledger lines reference rule_id (spec §6.4, §10.5).
    deleted_at       TIMESTAMPTZ,
    deleted_by       VARCHAR(200),
    row_version      INTEGER      NOT NULL DEFAULT 1,

    CONSTRAINT uq_billing_rule_logical_revision UNIQUE (logical_rule_id, revision),

    -- effective_from must precede effective_to once the rule is published (drafts
    -- may be saved in an invalid state and get validation feedback instead).
    -- A RETIRED revision that was retired/superseded before it ever took effect
    -- is stored with an empty window (effective_to = effective_from).
    CONSTRAINT ck_billing_rule_window CHECK (
        status IN ('DRAFT', 'DELETED')
        OR effective_to IS NULL OR effective_from IS NULL
        OR effective_to > effective_from
        OR (status = 'RETIRED' AND effective_to = effective_from)
    ),

    -- Anything that was published must be complete and attributed.
    CONSTRAINT ck_billing_rule_published_complete CHECK (
        status NOT IN ('PUBLISHED', 'RETIRED')
        OR (payer_type IS NOT NULL AND effective_from IS NOT NULL
            AND published_at IS NOT NULL AND published_by IS NOT NULL)
    ),
    CONSTRAINT ck_billing_rule_partner_reference CHECK (
        status NOT IN ('PUBLISHED', 'RETIRED') OR payer_type <> 'PARTNER' OR payer_reference IS NOT NULL
    ),
    CONSTRAINT ck_billing_rule_retired_attribution CHECK (
        status <> 'RETIRED' OR (retired_at IS NOT NULL AND retired_by IS NOT NULL)
    ),
    CONSTRAINT ck_billing_rule_deleted_attribution CHECK (
        status <> 'DELETED' OR (deleted_at IS NOT NULL AND deleted_by IS NOT NULL)
    )
);

COMMENT ON TABLE billing_rule IS
    'Versioned billing rules. Published business fields are immutable; changes create a new revision (spec §6.1, §13).';
COMMENT ON COLUMN billing_rule.logical_rule_id IS 'Stable identifier across revisions.';
COMMENT ON COLUMN billing_rule.row_version IS 'Optimistic-lock version, bumped automatically on every UPDATE.';

-- At most one open draft per logical rule.
CREATE UNIQUE INDEX ux_billing_rule_one_draft
    ON billing_rule (logical_rule_id) WHERE status = 'DRAFT';

-- §15 indexes
CREATE INDEX ix_billing_rule_merchant_fee ON billing_rule (merchant_id, fee_type, status);
CREATE INDEX ix_billing_rule_logical      ON billing_rule (logical_rule_id, revision);
CREATE INDEX ix_billing_rule_status       ON billing_rule (status);
CREATE INDEX ix_billing_rule_effective    ON billing_rule (effective_from, effective_to);

-- -----------------------------------------------------------------------------
-- §6.2 billing_rule_condition — one row per allowed value.
-- A dimension that applies to ALL values is simply absent.
-- -----------------------------------------------------------------------------

CREATE TABLE billing_rule_condition (
    condition_id     UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id          UUID         NOT NULL REFERENCES billing_rule (rule_id) ON DELETE CASCADE,
    dimension_key    VARCHAR(64)  NOT NULL REFERENCES billing_dimension_catalog (dimension_key),
    -- Canonicalized: trimmed, upper-case, non-empty.
    dimension_value  VARCHAR(128) NOT NULL CHECK (dimension_value ~ '^[A-Z0-9][A-Z0-9_\-]*$'),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_billing_rule_condition UNIQUE (rule_id, dimension_key, dimension_value)
);

CREATE INDEX ix_billing_rule_condition_dim ON billing_rule_condition (dimension_key, dimension_value);

-- -----------------------------------------------------------------------------
-- §6.4 billing_rule_audit — append-only
-- No FK to billing_rule: the audit trail must outlive any row-level cleanup.
-- -----------------------------------------------------------------------------

CREATE TABLE billing_rule_audit (
    audit_id         BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule_id          UUID         NOT NULL,
    logical_rule_id  UUID         NOT NULL,
    action           VARCHAR(16)  NOT NULL CHECK (action IN
                         ('CREATED', 'UPDATED', 'PUBLISHED', 'SUPERSEDED', 'RETIRED', 'REJECTED', 'DELETED')),
    actor            VARCHAR(200) NOT NULL,
    occurred_at      TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(),
    previous_value   JSONB,
    new_value        JSONB,
    reason           TEXT
);

CREATE INDEX ix_billing_rule_audit_logical ON billing_rule_audit (logical_rule_id, occurred_at);

-- =============================================================================
-- Integrity triggers
-- =============================================================================

-- Published/retired rows: business fields frozen, effective_to may only shrink,
-- status may only move forward (DRAFT -> PUBLISHED -> RETIRED, any -> DELETED),
-- never hard-deleted. DELETED is terminal. Bumps row_version.
CREATE FUNCTION trg_billing_rule_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN
            RAISE EXCEPTION 'Rule % is %; published rule versions cannot be hard-deleted (set status DELETED)', OLD.rule_id, OLD.status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'DELETED' OR (OLD.status = 'RETIRED' AND NEW.status <> 'DELETED') THEN
        RAISE EXCEPTION 'Rule % is % and cannot be modified', OLD.rule_id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NOT (
        (OLD.status = NEW.status)
        OR (OLD.status = 'DRAFT'     AND NEW.status = 'PUBLISHED')
        OR (OLD.status = 'PUBLISHED' AND NEW.status = 'RETIRED')
        OR (NEW.status = 'DELETED')
    ) THEN
        RAISE EXCEPTION 'Invalid status transition % -> % for rule %', OLD.status, NEW.status, OLD.rule_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF OLD.status IN ('PUBLISHED', 'RETIRED') THEN
        IF (NEW.logical_rule_id, NEW.revision, NEW.merchant_id, NEW.fee_type, NEW.payer_type,
            NEW.payer_reference, NEW.is_waived, NEW.effective_from, NEW.change_reason,
            NEW.created_at, NEW.created_by, NEW.published_at, NEW.published_by)
           IS DISTINCT FROM
           (OLD.logical_rule_id, OLD.revision, OLD.merchant_id, OLD.fee_type, OLD.payer_type,
            OLD.payer_reference, OLD.is_waived, OLD.effective_from, OLD.change_reason,
            OLD.created_at, OLD.created_by, OLD.published_at, OLD.published_by)
        THEN
            RAISE EXCEPTION 'Published business fields of rule % cannot be edited in place; create a new revision', OLD.rule_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        IF NEW.status = 'DELETED'
           AND (NEW.effective_to, NEW.retired_at, NEW.retired_by)
               IS DISTINCT FROM (OLD.effective_to, OLD.retired_at, OLD.retired_by)
        THEN
            RAISE EXCEPTION 'Deleting rule % cannot change its effective window or retirement', OLD.rule_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        IF NEW.effective_to IS DISTINCT FROM OLD.effective_to
           AND NOT (NEW.effective_to IS NOT NULL
                    AND (OLD.effective_to IS NULL OR NEW.effective_to < OLD.effective_to))
        THEN
            RAISE EXCEPTION 'effective_to of published rule % can only be shortened (supersession/retirement)', OLD.rule_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;

    NEW.row_version := OLD.row_version + 1;
    NEW.updated_at  := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER billing_rule_guard
    BEFORE UPDATE OR DELETE ON billing_rule
    FOR EACH ROW EXECUTE FUNCTION trg_billing_rule_guard();

-- §6.1: published revisions of the same logical rule cannot overlap.
-- Advisory lock serialises concurrent publishes of the same logical rule.
CREATE FUNCTION trg_billing_rule_no_overlap() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    clash RECORD;
BEGIN
    IF NEW.status NOT IN ('PUBLISHED', 'RETIRED') THEN
        RETURN NULL;
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.logical_rule_id::text, 0));

    SELECT rule_id, revision, effective_from, effective_to INTO clash
    FROM billing_config.billing_rule o
    WHERE o.logical_rule_id = NEW.logical_rule_id
      AND o.rule_id <> NEW.rule_id
      AND o.status IN ('PUBLISHED', 'RETIRED')
      AND tstzrange(o.effective_from, o.effective_to, '[)')
          && tstzrange(NEW.effective_from, NEW.effective_to, '[)')
    LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION 'Revision % overlaps published revision % (% to %) of the same logical rule',
            NEW.revision, clash.revision, clash.effective_from, coalesce(clash.effective_to::text, 'open')
            USING ERRCODE = 'exclusion_violation';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER billing_rule_no_overlap
    AFTER INSERT OR UPDATE ON billing_rule
    DEFERRABLE INITIALLY IMMEDIATE
    FOR EACH ROW EXECUTE FUNCTION trg_billing_rule_no_overlap();

-- Conditions are only writable while their rule is a DRAFT.
CREATE FUNCTION trg_billing_rule_condition_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    parent_status VARCHAR;
BEGIN
    SELECT status INTO parent_status
    FROM billing_config.billing_rule
    WHERE rule_id = CASE WHEN TG_OP = 'DELETE' THEN OLD.rule_id ELSE NEW.rule_id END;

    -- Parent already gone => cascading delete of a discarded draft.
    IF parent_status IS NOT NULL AND parent_status <> 'DRAFT' THEN
        RAISE EXCEPTION 'Conditions of a % rule cannot be changed', parent_status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.rule_id <> OLD.rule_id THEN
        RAISE EXCEPTION 'Conditions cannot be moved between rules'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE TRIGGER billing_rule_condition_guard
    BEFORE INSERT OR UPDATE OR DELETE ON billing_rule_condition
    FOR EACH ROW EXECUTE FUNCTION trg_billing_rule_condition_guard();

-- Audit history is append-only.
CREATE FUNCTION trg_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (% rejected)', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$;

CREATE TRIGGER billing_rule_audit_append_only
    BEFORE UPDATE OR DELETE ON billing_rule_audit
    FOR EACH ROW EXECUTE FUNCTION trg_append_only();

CREATE TRIGGER billing_rule_audit_no_truncate
    BEFORE TRUNCATE ON billing_rule_audit
    FOR EACH STATEMENT EXECUTE FUNCTION trg_append_only();

CREATE TRIGGER billing_rule_no_truncate
    BEFORE TRUNCATE ON billing_rule
    FOR EACH STATEMENT EXECUTE FUNCTION trg_append_only();
