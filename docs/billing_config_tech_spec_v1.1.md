# OpenBorder — Billing Configuration System

**Technical Specification | Draft v1.1**

**Status:** Proposed
**Last updated:** 2026-10-20
**Primary owners:** Engineering (application), Data (billing logic and warehouse models)
**Recommended technology:** PostgreSQL, backend API, Snowflake, dbt, Prefect

**Changes in v1.1**

- Explicit rule priority removed. BR-07 now defines precedence by *exceptions*: a rule can override another only by constraining additional dimensions. Any other overlap is rejected at publish time.
- Admin-only rule deletion added (BR-08). It is a soft delete with a full audit trail.
- Clarified that ALL is represented by the absence of condition rows (§6.2).

---

## 1. Executive Summary

OpenBorder requires a configurable billing system that determines who is financially responsible for OMS-calculated fees and whether those fees should be waived.

The system must support billing rules based on a flexible set of order attributes, including:

- Merchant
- Fee type
- Destination country
- Origin country
- Carrier
- Fulfillment center (FC) code
- Additional dimensions introduced in the future

Each optional dimension must support applying a rule to all values or selecting multiple specific values.

**Proposed architecture:**

- **PostgreSQL:** Source of truth for billing configuration.
- **Configuration UI and API:** Engineering-owned application for managing, validating, and publishing rules.
- **Configuration synchronization:** Data-owned process that replicates published rules to Snowflake.
- **Snowflake + dbt:** Executes generic configuration matching and determines payer and waiver treatment.
- **Billing ledger:** Preserves the configuration applied, original fee amount, final billed amount, and billing history.

The billing engine must not require SQL changes whenever a new matching dimension or new value is introduced, provided that the new dimension is available in the staged order attributes.

---

## 2. Scope

### 2.1 In Scope

1. Configuration rule creation, editing, publishing, retirement, deletion, and viewing.
2. Multi-dimensional rule matching.
3. Multiple allowed values within each dimension.
4. Rule precedence and conflict prevention.
5. Fee payer and waiver configuration.
6. Effective dating and rule versioning.
7. API validation and authentication.
8. PostgreSQL configuration storage.
9. Synchronization of published configuration into Snowflake.
10. Generic SQL-based rule matching.
11. Auditability, monitoring, and error handling.

### 2.2 Out of Scope

- Calculating shipping fees, duties, taxes, or other OMS fees.
- OMS rate-card configuration.
- Modifying OMS pricing logic.
- Generating external invoice PDFs.
- Payment collection.
- Arbitrary fee-amount overrides.
- Splitting a single fee amount across multiple payers.
- Complex nested Boolean expressions or custom scripting inside billing rules.

---

## 3. Responsibility Matrix

| Component | Owner | Responsibility |
|---|---|---|
| Rate-card configuration | Engineering | Maintain OMS pricing configuration |
| OMS fee calculation | Engineering | Calculate all applicable order fees |
| Externally processed order identification | Engineering | Identify orders requiring special OMS processing |
| Billing configuration UI | Engineering | Build the application and user experience |
| Configuration backend/API | Engineering | Validation, persistence, permissions, publishing |
| PostgreSQL database | Engineering | Provisioning, availability, backups, migrations |
| Billing rule definitions/policies | Data | Define payer/waiver semantics and matching behavior |
| Configuration data quality | Data + Engineering | Business rule testing and application validation |
| PostgreSQL → Snowflake sync | Data | Replicate published configuration consistently |
| OMS staging | Data | Normalize fee records and expose matchable order attributes |
| Rule-matching SQL | Data | Resolve the correct configuration for each fee |
| Billing ledger/history | Data | Preserve final billing decisions and adjustments |
| Automated billing checks | Data | Detect fee anomalies, missing configs, and mismatches |
| OMS rerun API | Engineering | Provide reliable, idempotent rerun capability |
| Automated rerun orchestration | Data | Request reruns for eligible OMS errors and revalidate results |

---

## 4. Business Requirements

### BR-01: Fee-level configuration

Each configuration applies to a specific merchant and fee type.

Initial supported fee types:

- `platform_fee`
- `shipping_fee`
- `deferment_fee`
- `duties`
- `taxes`

New fee types must be supported through reference data and configuration without modifying the matching algorithm.

### BR-02: Dynamic matching dimensions

A rule can contain zero or more matching dimensions.

Initial dimensions:

| Dimension key | Description | Example |
|---|---|---|
| `DESTINATION_COUNTRY` | Order destination | CA, US, MX |
| `ORIGIN_COUNTRY` | Country of origin | CN, US, VN |
| `CARRIER` | Canonical OMS carrier identifier | DHL_EXPRESS |
| `FC_CODE` | Fulfillment center identifier | HKG01 |

Carrier and FC values are not hard-coded enums. Their available values should come from a canonical source maintained by OMS or the corresponding reference-data service.

### BR-03: Multiple allowed values

Each dimension supports one or more allowed values.

**Matching semantics:**

- OR between values of the same dimension.
- AND between distinct dimensions.
- An omitted dimension means ALL values.

Example:

```
Destination = CA, US, MX
Carrier = DHL, FedEx
Origin = ALL
FC = HKG01
```

Equivalent logic:

```
destination IN (CA, US, MX) AND carrier IN (DHL, FedEx) AND fc_code = HKG01
```

This produces independent combinations. Rules requiring a particular pairing of country and carrier must be represented by separate rules.

### BR-04: Payer configuration

Each rule specifies:

- `payer_type`: MERCHANT, CARRIER, or PARTNER
- `payer_reference`: optional reference to a specific payer entity

Recommended interpretation:

| Payer type | Resolution |
|---|---|
| MERCHANT | Merchant associated with the order |
| CARRIER | Carrier associated with the fee/order |
| PARTNER | Explicitly configured partner |

The carrier used as a rule-matching dimension must remain logically separate from the carrier that receives financial responsibility.

### BR-05: Fee waiver

Each rule contains `is_waived`.

If true:

- Preserve the original OMS fee.
- Set the amount to invoice to zero.
- Record the waived amount.
- Preserve the rule and intended payer for audit.

If false:

- Use the original OMS fee amount.
- Assign the configured payer.

A waiver must not alter the OMS source record.

### BR-06: Time-dependent configuration

Rules support:

- Effective start timestamp, inclusive.
- Effective end timestamp, exclusive.
- Scheduling future-dated changes.
- Historical configuration retrieval.

Billing must evaluate the rule against an agreed billing event timestamp, not simply the current processing time.

### BR-07: Deterministic selection

For each fee line, exactly one winning configuration must be selected. Rules have no explicit priority. The publish-time policy below guarantees a single winner.

Two published rules **overlap** when all of the following hold:

- They have the same merchant and fee type.
- Their effective windows overlap.
- At least one fee line could match both. This means every dimension constrained by both rules shares at least one value. A dimension constrained by only one rule never prevents overlap, because the other rule accepts ALL values there.

Overlapping rules are allowed only when one is an **exception** of the other. The exception constrains every dimension the other constrains, plus at least one dimension the other leaves as ALL. Where both match, the exception wins.

Every other overlap is a **conflict** and blocks publishing:

- Both rules constrain the same dimensions and share at least one value.
- The rules constrain different dimensions, and neither is an exception of the other.

Rules that can never match the same fee line do not conflict, even when they share values in a dimension.

Examples (same merchant and fee type, overlapping effective windows):

| Rule A | Rule B | Result | Reason |
|---|---|---|---|
| Destination = CA, AU | Destination = AU, GB | **Conflict** | Same dimension; both specify AU |
| Destination = CA, AU | Destination = AU | **Conflict** | Same dimension; both specify AU |
| Destination = CA (waived) | No conditions (payer: Carrier) | Allowed; A wins for CA | A adds Destination on top of ALL |
| R101: Destination = CA, US, MX; Carrier = DHL_EXPRESS, FEDEX | R102: Destination = CA; Carrier = DHL_EXPRESS; Origin = CN; FC = HKG01 | Allowed; R102 wins where both match | R102 adds Origin and FC |
| Destination = CA; Carrier = DHL_EXPRESS | Destination = CA; Carrier = FEDEX | Allowed | They never match the same fee line |
| Destination = CA | Carrier = DHL_EXPRESS | **Conflict** | Both match CA + DHL_EXPRESS; neither is an exception |
| Destination = CA, AU | Destination = AU, GB; Origin = CN | Allowed; B wins for AU + CN | B adds Origin |

**Consequence for matching.** Any two valid rules that can match the same fee line constrain strictly nested sets of dimensions. The matching engine therefore selects the rule with the most constrained dimensions, and a tie cannot occur in valid configuration. A tie at run time indicates a configuration defect and is raised as a billing exception (§12.1).

**Conflict checks are pairwise.** "Destination = CA" and "Carrier = DHL_EXPRESS" cannot both be published, even if a third rule (Destination = CA; Carrier = DHL_EXPRESS) would decide their overlap. Until a NOT IN operator exists (Phase 3), such cases are expressed by listing values explicitly. For example, the carrier rule can list every destination except CA.

Validation also reports, for information, which existing rules the rule overrides or is overridden by.

### BR-08: Rule deletion

Admins can delete a rule that was configured in error. Deletion is distinct from retirement:

| | Retire | Delete |
|---|---|---|
| Who | Publisher, Admin | Admin only |
| Effect | Stops applying from a chosen timestamp | Stops applying for all dates |
| Historical window | Kept; late billing events still match it | Removed from future billing runs |
| Scope | One published revision | A draft: only that draft. A published or retired rule: all revisions of the logical rule |

Deletion rules:

- A reason is required and is recorded in the audit trail.
- It is a soft delete. Status becomes DELETED, and the rule and its audit history are retained, because configuration snapshots and billing ledger lines reference `rule_id` (§6.4, §10.5).
- From the next configuration snapshot, the rule is excluded from synchronization, matching, and conflict checks.
- Fee lines that are not yet finalized and were matched to the deleted rule are re-evaluated on the next billing run.
- Finalized invoices are never altered. Corrections to finalized amounts follow §13.
- It is irreversible. A deleted rule can be duplicated as a new rule but cannot receive new revisions.

---

## 5. Architecture

### 5.1 Configuration write path

**Frontend → Backend API → PostgreSQL**

The frontend must not connect directly to PostgreSQL or Snowflake.

The backend is responsible for:

- Authentication and authorization.
- Validating supported fee and dimension identifiers.
- Creating and editing drafts.
- Detecting duplicate conditions.
- Rejecting conflicting rules (BR-07).
- Publishing configurations transactionally.
- Deleting rules (Admin only, BR-08).
- Recording audit history.

### 5.2 Warehouse read path

**PostgreSQL → Prefect Sync → Snowflake Configuration Models → Billing SQL**

The Data pipeline reads only published configuration.

Published rules and their conditions must be transferred as a consistent snapshot.

A billing run must use one configuration snapshot throughout the entire run.

### 5.3 Core principle

PostgreSQL controls **what the billing rules are**.

Snowflake controls **how those rules are applied to OMS fees and recorded for financial reporting**.

No synchronous call to the configuration API should be required while Snowflake is processing an order.

---

## 6. PostgreSQL Data Model

### 6.1 Table: `billing_rule`

Each record represents a particular version of a billing rule.

| Field | PostgreSQL type | Description |
|---|---|---|
| rule_id | UUID | Primary key for the rule version |
| logical_rule_id | UUID | Stable identifier across revisions |
| revision | INTEGER | Revision number |
| merchant_id | VARCHAR | Canonical merchant identifier |
| fee_type | VARCHAR | Fee being configured |
| payer_type | VARCHAR NULL | MERCHANT / CARRIER / PARTNER; nullable only while DRAFT |
| payer_reference | VARCHAR NULL | Explicit payer when required |
| is_waived | BOOLEAN | Whether the fee should be waived |
| effective_from | TIMESTAMPTZ NULL | Effective timestamp, inclusive; nullable only while DRAFT |
| effective_to | TIMESTAMPTZ NULL | End timestamp, exclusive |
| status | VARCHAR | DRAFT / PUBLISHED / RETIRED / DELETED |
| change_reason | TEXT NULL | Reason entered in the editor |
| created_at | TIMESTAMPTZ | Creation timestamp |
| created_by | VARCHAR | Creating user |
| updated_at | TIMESTAMPTZ | Update timestamp |
| published_at | TIMESTAMPTZ NULL | Publication timestamp |
| published_by | VARCHAR NULL | Publishing user |
| retired_at | TIMESTAMPTZ NULL | Retirement timestamp |
| retired_by | VARCHAR NULL | Retiring user |
| deleted_at | TIMESTAMPTZ NULL | Deletion timestamp |
| deleted_by | VARCHAR NULL | Deleting user (Admin) |
| row_version | INTEGER | Optimistic-lock version |

**Constraints**

- Unique `logical_rule_id` + `revision`.
- At most one DRAFT per logical rule.
- `effective_from` must precede `effective_to` for published rules. A revision retired or superseded before it took effect is stored with an empty window (`effective_to = effective_from`).
- Valid payer type and status.
- Published and retired rules must have a payer, `effective_from`, and publication attribution. PARTNER requires `payer_reference`.
- Draft edits require a matching `row_version`, which is incremented on every update.
- Published business fields cannot be edited in place. `effective_to` may only be shortened, by supersession or retirement.
- Status moves only DRAFT → PUBLISHED → RETIRED, or from any status to DELETED. DELETED is terminal, and deleting cannot change a revision's effective window.
- Published, retired, and deleted versions cannot be hard-deleted.
- Published revisions of the same logical rule cannot have overlapping effective windows.

Conflicts between different logical rules (BR-07) are validated by the backend at publish time rather than by a database constraint, because they depend on the condition sets.

### 6.2 Table: `billing_rule_condition`

Stores the allowed values associated with a rule.

| Field | PostgreSQL type | Description |
|---|---|---|
| condition_id | UUID | Primary key |
| rule_id | UUID | Foreign key to billing_rule |
| dimension_key | VARCHAR | Attribute to match |
| dimension_value | VARCHAR | Allowed canonical value |
| created_at | TIMESTAMPTZ | Audit timestamp |

**Constraints**

- Unique `(rule_id, dimension_key, dimension_value)`.
- Valid rule reference.
- Registered dimension key.
- Canonicalized, nonempty dimension value.

A dimension with three allowed countries is stored as three condition records.

A dimension applying to ALL values is **not stored** in this table.

A rule with zero conditions matches all values of all optional dimensions, provided its merchant, fee type, and effective date match.

ALL is represented by the absence of rows:

- A dimension with no condition rows for a rule applies to ALL values of that dimension.
- A rule with no condition rows applies to every order for its merchant, fee type, and effective window. Consumers must not inner-join rules to conditions, because that drops these rules.
- A "Specific" scope with zero values is rejected on save rather than stored, because zero rows would silently mean ALL.
- When a new dimension is registered, existing rules apply to ALL of its values without data migration.

Example (merchant M123, duties):

| Rule | dimension_key | dimension_value |
|---|---|---|
| Default (no conditions) | *(no rows)* | |
| R101 | CARRIER | DHL_EXPRESS |
| R101 | CARRIER | FEDEX |
| R101 | DESTINATION_COUNTRY | CA |
| R101 | DESTINATION_COUNTRY | MX |
| R101 | DESTINATION_COUNTRY | US |

R101 has no Origin country or FC code rows, so it applies to all values of both.

### 6.3 Table: `billing_dimension_catalog`

A controlled registry of supported dimensions.

| Field | Description |
|---|---|
| dimension_key | Stable technical key |
| display_name | UI label |
| data_type | Initial support: STRING |
| value_source | Canonical source for selectable values |
| is_active | Whether the dimension can be used in new rules |
| description | Business meaning |

Example:

| dimension_key | display_name | value_source |
|---|---|---|
| DESTINATION_COUNTRY | Destination Country | Country reference |
| ORIGIN_COUNTRY | Origin Country | Country reference |
| CARRIER | Carrier | OMS carrier reference |
| FC_CODE | Fulfillment Center | OMS FC reference |

Adding a dimension requires registering it and making its attribute available in Snowflake order staging.

It does not require modifying the generic rule-matching SQL.

### 6.4 Table: `billing_rule_audit`

Append-only history of configuration changes.

Recommended fields:

- audit_id
- rule_id
- logical_rule_id
- action
- actor
- occurred_at
- previous_value (JSONB)
- new_value (JSONB)
- reason

Audit actions include CREATED, UPDATED, PUBLISHED, SUPERSEDED, RETIRED, REJECTED, and DELETED.

Published rule versions and audit records must not be hard-deleted. Deleting a rule is a status change (BR-08).

---

## 7. Example Configuration

### Rule R101

**Merchant:** M123
**Fee:** Duties
**Payer:** Merchant
**Waived:** Yes

**Conditions:**

| Dimension | Allowed values |
|---|---|
| Destination country | CA, US, MX |
| Carrier | DHL_EXPRESS, FEDEX |
| Origin country | All |
| FC code | All |

### Rule R102

**Merchant:** M123
**Fee:** Duties
**Payer:** Merchant
**Waived:** No

**Conditions:**

| Dimension | Allowed values |
|---|---|
| Destination country | CA |
| Carrier | DHL_EXPRESS |
| Origin country | CN |
| FC code | HKG01 |

An order matching R102 also matches R101. R102 wins because it is an exception of R101: it constrains every dimension R101 constrains, plus Origin country and FC code (BR-07).

---

## 8. Frontend Specification

### 8.1 Rule List Page

**Purpose:** Allow authorized users to discover, review, and manage billing configurations.

Required functionality:

- Search by merchant, fee type, and rule ID.
- Filter by status: Draft, Published, Retired, Deleted. Deleted is hidden by default.
- Filter by effective date.
- Filter rules by applicable carrier, country, or FC. Rules that apply to ALL values of that dimension are included.
- View payer and waiver behavior.
- View draft and published revisions.
- Create, duplicate, edit, retire, and delete rules, subject to permissions.

Recommended table columns:

| Column | Example |
|---|---|
| Merchant | M123 |
| Fee | Duties |
| Conditions | CA/US/MX, DHL/FedEx |
| Payer | Merchant |
| Waived | Yes |
| Effective date | 2026-11-01 |
| Status | Published |
| Actions | View / Duplicate / Retire / Delete (Admin) |

### 8.2 Rule Editor

The editor is divided into three sections.

**A. Rule identity**

- Merchant — searchable single-select
- Fee type — searchable single-select

**B. Applies When**

Dynamic list of available dimensions.

For each dimension:

- Display name.
- Scope: All or Specific.
- If Specific: searchable multi-select.
- Add/remove dimension.

Defaults:

- Unspecified dimensions apply to all values.
- Selecting Specific requires at least one value.
- Duplicate values are prevented.
- Only active, registered dimension keys may be selected.

The section displays the conflict policy (BR-07) next to the dimensions.

**C. Billing Decision**

- Payer type — select
- Payer entity — conditional field
- Waive fee — toggle
- Effective start/end
- Change reason

There is no priority field.

The editor should generate a human-readable summary:

> "For merchant M123, waive duties on Canada, US, or Mexico shipments using DHL or FedEx, regardless of origin country or FC."

### 8.3 Save and Publish

Required actions:

1. Save Draft
2. Validate
3. Publish
4. Duplicate as New Revision
5. Retire
6. View Change History
7. Delete (Admin only)

**Save Draft**

Persists incomplete configuration without affecting billing.

**Validate**

Checks rule structure, reference values, effective dates, and conflicts.

**Publish**

Makes a valid configuration eligible for synchronization into Snowflake.

**Duplicate as New Revision**

Copies a published rule into an editable draft without modifying the published version.

**Retire**

Stops future application of a rule from a controlled effective timestamp. Historical configuration remains available.

**View Change History**

Shows the audit trail for all revisions of the logical rule.

**Delete**

Removes a rule configured in error (BR-08). Deleting a draft removes only that draft. Deleting a published or retired rule removes all of its revisions. The confirmation lists the affected revisions, requires a reason, and explains how deletion differs from Retire.

### 8.4 Validation Feedback

Validation errors must be displayed before publishing.

Examples:

- Missing payer.
- Partner payer selected without a partner identifier.
- Specific carrier selected without any carrier value.
- Duplicate dimension values.
- Unsupported dimension.
- End date before start date.
- Same logical rule has overlapping effective versions.
- Conflicting rule: another rule for the same merchant and fee type specifies the same dimension values (e.g. both specify Destination AU).
- Conflicting rule: another rule overlaps using different dimensions, and neither is an exception of the other.
- Configured dimension is not yet available in billing staging.

Informational messages tell the user which existing rules the rule overrides as an exception, or which rules override it.

Blocking errors, warnings, and informational messages should be visually distinguishable.

### 8.5 Rule Preview

Recommended as a second-phase feature.

Allow the user to enter or select an example order and inspect:

- Matching rules.
- Selected rule.
- Reason for selection.
- Payer.
- Waiver treatment.
- Original OMS fee.
- Proposed invoice amount.

Financially authoritative results must come from the Data-owned matching implementation. A separate UI-side matcher must not become a second independent billing decision engine.

---

## 9. Backend API Specification

### 9.1 API Design

REST API, with JSON request/response payloads.

Proposed endpoints:

| Method | Endpoint | Description |
|---|---|---|
| GET | /billing/rules | List/search rules |
| GET | /billing/rules/{id} | Read rule and conditions |
| POST | /billing/rules | Create draft |
| PATCH | /billing/rules/{id} | Update draft |
| DELETE | /billing/rules/{id} | Delete rule (Admin only; soft delete; body: `row_version`, `reason`) |
| POST | /billing/rules/{id}/validate | Validate configuration |
| POST | /billing/rules/{id}/publish | Publish valid draft |
| POST | /billing/rules/{id}/retire | Retire configuration |
| POST | /billing/rules/{id}/clone | Create successor draft |
| GET | /billing/dimensions | List supported dimensions |
| GET | /billing/dimensions/{key}/values | Search canonical values |
| GET | /billing/rules/{id}/history | Audit/revision history |

Use application authentication rather than exposing PostgreSQL credentials to the browser.

### 9.2 Create/Update Payload

Proposed JSON contract:

```json
{
  "merchant_id": "M123",
  "fee_type": "duties",
  "payer_type": "MERCHANT",
  "payer_reference": null,
  "is_waived": true,
  "effective_from": "2026-11-01T00:00:00Z",
  "effective_to": null,
  "change_reason": "North America duty waiver promotion",
  "conditions": [
    { "dimension": "DESTINATION_COUNTRY", "operator": "IN", "values": ["CA", "US", "MX"] },
    { "dimension": "CARRIER", "operator": "IN", "values": ["DHL_EXPRESS", "FEDEX"] }
  ]
}
```

Each condition represents one dimension containing an array of allowed values.

The API expands those values into rows in `billing_rule_condition`.

An omitted dimension means ALL.

For v1, the only supported matching operator is `IN`. The operator is included in the API contract to make matching semantics explicit.

### 9.3 Backend Validation

Before publishing, the backend must verify:

1. The merchant exists.
2. The fee type is supported.
3. Every dimension is registered.
4. Dimension values are valid canonical identifiers.
5. No duplicate dimension/value pairs exist.
6. Payer configuration is valid.
7. Effective periods are valid.
8. The rule does not conflict with another published rule for the same merchant and fee type with an overlapping effective window (BR-07).
9. The referenced matching attributes are supported by the billing pipeline.

All rule and condition writes must occur in one PostgreSQL transaction.

### 9.4 Publishing Transaction

Publishing should:

1. Lock the draft/revision being published.
2. Verify its expected `row_version`.
3. Validate the complete rule and condition set.
4. Verify effective-period and conflict constraints.
5. Transition the rule to PUBLISHED.
6. Write its audit event.
7. Commit atomically.

For successor revisions, publishing must also handle supersession/end-dating of the previous revision in the same controlled transaction.

No partial rule/condition publication is allowed.

### 9.5 Authorization

Suggested roles:

| Role | Permissions |
|---|---|
| Viewer | View configuration and history |
| Editor | Create and modify drafts |
| Publisher | Publish and retire rules |
| Admin | Delete rules; manage access, reference configuration, and exceptional operations |

All changes must record the authenticated user identity.

Publishing access should be more restricted than draft editing. Deletion is more restricted than publishing. In v1, discarding a draft is a deletion and is therefore Admin-only (see §18).

### 9.6 Deletion Transaction

Deleting should:

1. Lock the target revision.
2. Verify its expected `row_version`.
3. Require a non-empty reason.
4. Determine the scope: the draft alone, or every non-deleted revision of the logical rule.
5. Set each affected revision to DELETED with `deleted_at` and `deleted_by`, leaving its effective window unchanged.
6. Write a DELETED audit event per revision, holding the previous state and the reason.
7. Commit atomically.

---

## 10. PostgreSQL → Snowflake Synchronization

### 10.1 Ownership

Data owns the synchronization process.

Engineering owns availability and correctness of the PostgreSQL configuration API/database.

### 10.2 Proposed Implementation

For MVP, use a scheduled Prefect flow.

1. Read the published configuration and conditions from PostgreSQL using a consistent database transaction/snapshot.
2. Assign a unique `config_snapshot_id`.
3. Load both rule and condition datasets to staging tables in Snowflake.
4. Validate record counts, uniqueness, referential integrity, and snapshot consistency.
5. Mark the snapshot as ready only if validation succeeds.
6. Make the ready snapshot available for subsequent billing runs.

Because configuration datasets are expected to be relatively small, a complete published-configuration snapshot is simpler initially than implementing CDC for configuration changes.

The schedule can begin at 5–15 minutes, subject to business requirements and database capacity.

### 10.3 Snowflake Tables

Recommended logical datasets:

**`cfg_billing_rule_snapshot`**

- config_snapshot_id
- rule_id
- merchant_id
- fee_type
- payer_type
- payer_reference
- is_waived
- effective_from
- effective_to
- published_at

The snapshot includes PUBLISHED revisions, plus RETIRED revisions with a non-empty effective window (so late billing events still resolve). DRAFT and DELETED rules are never synchronized.

**`cfg_billing_rule_condition_snapshot`**

- config_snapshot_id
- rule_id
- dimension_key
- dimension_value

**`cfg_billing_snapshot_manifest`**

- config_snapshot_id
- extracted_at
- validated_at
- status
- rule_count
- condition_count
- content_hash

All downstream models must select rules and conditions belonging to the same `config_snapshot_id`.

A failed synchronization must not expose a partially loaded snapshot.

### 10.4 Consistency Requirements

- PostgreSQL is authoritative for the rule definition.
- Snowflake uses the latest fully validated snapshot.
- A billing run pins a single snapshot ID.
- Published changes become usable only after synchronization completes.
- Sync failures and excessive snapshot age trigger alerts.
- Final billing must be blocked if configuration freshness or completeness violates the agreed SLA.

### 10.5 Configuration History

Do not delete previous configuration snapshots needed for audit and reproduction.

Every finalized billing fee line should include:

- rule_id
- config_snapshot_id
- billing_run_id

This enables reconstruction of the configuration used for a historical invoice.

---

## 11. Generic Snowflake Matching Engine

### 11.1 OMS Staging Contract

Data transforms OMS-calculated fees into fee-level staging records.

Required fields:

| Field | Description |
|---|---|
| fee_line_id | Unique ID for the fee component |
| order_id | Order ID |
| merchant_id | Merchant |
| fee_type | Fee type |
| source_amount | OMS-calculated amount |
| currency | Amount currency |
| billing_event_at | Effective-rule matching timestamp |
| match_context | Semi-structured object of order dimensions |

Example `match_context`:

```json
{
  "DESTINATION_COUNTRY": "CA",
  "ORIGIN_COUNTRY": "CN",
  "CARRIER": "DHL_EXPRESS",
  "FC_CODE": "HKG01"
}
```

The matching engine must not rely on hard-coded individual dimension columns.

### 11.2 Matching Algorithm

For each fee line:

1. Find candidate rules by merchant, fee type, effective date, and pinned configuration snapshot.
2. Retrieve the conditions attached to each candidate rule.
3. For each dimension, determine whether at least one allowed value matches the staged order attribute.
4. A rule matches only if all its constrained dimensions match.
5. Select the matching rule with the most constrained dimensions.
6. If more than one matching rule has the same number of constrained dimensions, raise a configuration exception. Publish-time validation (BR-07) should make this impossible.
7. Return payer, waiver, and selected rule.
8. Apply the billing decision without recalculating OMS fees.

**Critical behavior:**

- Multiple values within one dimension count as one required dimension.
- Missing values in order context do not satisfy specific-value conditions.
- Rules without conditions act as all-dimension defaults. They have zero constrained dimensions and lose to any other matching rule.
- Candidate selection must start from rules, not conditions, so that rules without condition rows are not dropped.
- Missing mandatory order attributes should be validated independently so a broad default does not conceal bad OMS data.
- No matching rule results in a billing exception, not an implicit merchant charge.

### 11.3 Billing Decision

The output includes:

- Original OMS amount.
- Payer type and resolved payer identifier.
- Waiver flag.
- Waived amount.
- Invoice amount.
- Selected rule.
- Configuration snapshot.
- Billing run.

For waived fees:

```
invoice_amount = 0
waived_amount = source_amount
```

For non-waived fees:

```
invoice_amount = source_amount
waived_amount = 0
```

### 11.4 New Dimension Support

To introduce `SERVICE_LEVEL`:

1. Register the dimension in PostgreSQL.
2. Make the canonical service level available in OMS staging's `match_context`.
3. Verify staging coverage and normalization.
4. Enable it in the UI.
5. Publish rules using the dimension.

No change is required to the generic Snowflake condition-matching SQL.

---

## 12. Validation, Errors, and Billing Checks

### 12.1 Configuration Errors

Examples:

- Missing matching rule.
- Multiple matching rules with the same number of constrained dimensions. This indicates a gap in BR-07 validation.
- Invalid payer.
- Unknown dimension.
- Invalid effective-date overlap.
- Missing required source attribute.
- Missing configuration snapshot.

Affected fee lines must not finalize.

### 12.2 OMS Errors

Examples:

- OMS did not calculate required fees.
- Fee values are stale or missing.
- Incorrect externally processed order identification.
- An OMS source error requires recalculation.

For these errors, Data may call the Engineering-owned OMS rerun API.

### 12.3 Rerun Workflow

**OMS-related failure**

1. Data identifies the affected order.
2. Data sends an idempotent rerun request to the OMS rerun API.
3. Engineering's OMS process recalculates or republishes the affected source fees.
4. Data refreshes OMS staging.
5. Data repeats validation and billing matching.
6. Valid records continue to the ledger.

**Configuration-related failure**

1. Data identifies the missing or conflicting configuration.
2. The rule is corrected and published through the UI.
3. The new configuration is synchronized into Snowflake.
4. Data re-evaluates affected fee lines.

A configuration-only error should not require an OMS fee recalculation.

### 12.4 Required Monitoring

- Failed PostgreSQL → Snowflake synchronization.
- Snapshot freshness outside SLA.
- Missing billing rules.
- Ambiguous winning rules.
- Unsupported dimensions or values.
- OMS fee anomalies.
- Unexpected fee waivers.
- Rule deletions affecting non-finalized fee lines.
- Changes to finalized order fees.
- Rerun API failures or exhausted retries.
- Billing totals and reconciliation differences.

---

## 13. Rule Versioning and Billing History

Published configuration must not be silently overwritten.

Recommended behavior:

- Drafts can be edited.
- Published versions preserve their business fields.
- Changes create new rule revisions.
- Effective dates determine which revision applies.
- Retirement and supersession are audited.
- Retroactive changes require explicit authorization and impact assessment.
- Deletion is Admin-only and audited. It removes the rule from future billing runs for all dates and never rewrites finalized invoices (BR-08).

Billing ledger records must remain immutable after invoice finalization.

Configuration changes to historical periods must never silently rewrite previously finalized invoices.

Corrections should flow through an explicit credit, adjustment, or rebilling process.

---

## 14. Security and Operational Requirements

- Backend-to-PostgreSQL connections use managed credentials.
- UI clients must never receive database credentials.
- TLS must be used for database and API connections.
- Data synchronization uses a read-only PostgreSQL role.
- Application write permissions are scoped to configuration tables.
- Changes are attributed to authenticated users.
- Production and nonproduction configurations are isolated.
- PostgreSQL backups and recovery procedures must be established.
- Retention for rule versions, audit events, and billing snapshots must meet Finance requirements.
- Publication requests require optimistic concurrency control to prevent lost updates.
- Sensitive operational failures must be logged without exposing credentials or internal tokens.

---

## 15. Performance and Scalability

The rule configuration dataset is expected to be substantially smaller than the OMS order-fee dataset.

Optimization should focus on Snowflake candidate-rule matching.

Recommendations:

- Filter candidate rules using merchant, fee type, and effective dates before condition matching.
- Limit processing to changed or eligible fee lines.
- Reuse a published configuration snapshot across the billing run.
- Avoid repeatedly calling PostgreSQL for individual orders.
- Persist final matching decisions where necessary for audit.
- Monitor candidate-rule counts and billing query performance.
- Maintain appropriate PostgreSQL indexes for merchant, fee type, logical rule ID, publication status, and effective dates.

The initial implementation should prioritize correctness and auditability over premature SQL optimization.

---

## 16. MVP Acceptance Criteria

### Configuration

- An authorized user can create, edit, validate, and publish a billing rule.
- A rule may specify multiple destination countries.
- A rule may specify multiple origin countries.
- A rule may specify multiple carriers.
- A rule may specify multiple FC codes.
- Any optional dimension may be set to ALL.
- Payer and waiver fields can be configured independently of rule-matching dimensions.
- Invalid rules cannot be published without resolution.
- A rule that conflicts with another published rule under BR-07 cannot be published. Example: Destination = CA, AU vs Destination = AU, GB.
- An exception rule can be published over a broader rule. Example: Destination = CA over a rule with no conditions.
- An Admin can delete a rule. No other role can.
- Deleted rules are retained with their audit history, and are excluded from synchronization, matching, and conflict checks.
- Published configuration history is retained.

### Data Synchronization

- Published rules and conditions are available in Snowflake.
- Each sync produces a validated snapshot ID.
- Billing processing uses one consistent configuration snapshot.
- Failed syncs never expose incomplete rule sets.

### Matching Engine

- Rule matching is dimension-agnostic.
- OR-within-dimension and AND-across-dimension matching work correctly.
- Among matching rules, the rule with the most constrained dimensions (the exception) wins.
- Missing and ambiguous matches generate exceptions.
- A new dimension can be supported without changing the generic matching SQL.
- Every billing decision records its applied rule and snapshot.

### Audit and Reliability

- Drafts do not affect production billing.
- Published changes are auditable.
- Deletions are Admin-only, audited, and never alter finalized invoices.
- Historical finalized amounts cannot be silently rewritten.
- Billing reruns are idempotent.
- OMS rerun failures and configuration errors follow separate recovery paths.

---

## 17. Proposed Implementation Phases

### Phase 1 — Configuration MVP

Engineering:

- PostgreSQL schema
- Configuration API
- Rule list/editor UI
- Basic validation, conflict rejection, and publishing
- Admin rule deletion
- Authentication and audit trail

Data:

- Dimension and payer business definitions
- Configuration synchronization
- Snowflake rule-matching model
- Missing/ambiguous-rule checks
- Initial billing ledger integration

### Phase 2 — Operational Controls

Engineering:

- Advanced rule filtering
- Conflict visualization
- Rule history and comparison
- Improved value selectors
- Rerun API integration

Data:

- Automated OMS exception handling
- Rule-impact monitoring
- Historical billing reconciliation
- Automated rerun orchestration
- Configuration and billing freshness alerts

### Phase 3 — Advanced Rule Management

Potential enhancements:

- Rule simulation against historical orders.
- Publish approval workflow.
- Estimated financial impact before publishing.
- Bulk rule import/export.
- Carrier/FC groups.
- Additional operators such as NOT IN. These would also simplify expressing cross-dimension cases under BR-07.
- Relaxed conflict resolution across different dimension sets, such as allowing two rules to coexist when a third rule decides their overlap, or explicit priority, if a concrete need appears.

These features should only be added once the simpler condition model has demonstrated a concrete need for them.

---

## 18. Open Design Decisions

The following require Product, Engineering, and Data alignment before production:

1. **Billing event timestamp:** Order creation, shipment, fulfillment, or another agreed date?
2. **Fee granularity:** Are fees always order-level, or can a single order contain multiple carrier/shipment fee components?
3. **Carrier payer semantics:** Does CARRIER always mean the carrier attached to the fee, or can a different carrier be financially responsible?
4. **Rule priority:** *Resolved in v1.1.* There is no explicit priority; precedence is by exceptions per BR-07.
5. **Default rules:** Must each merchant and fee type have a catch-all rule, or can missing configurations block billing?
6. **Retroactive changes:** Who can approve them, and what existing billing records must be reevaluated?
7. **Publication workflow:** Is one authorized publisher sufficient, or is Finance approval required?
8. **Synchronization latency:** How soon must newly published rules affect billing?
9. **Canonical reference values:** Which OMS/reference systems own carrier IDs, FC codes, countries, and partner IDs?
10. **Configuration storage:** Use an existing managed PostgreSQL deployment where available; otherwise provision a dedicated database.
11. **Cross-dimension conflicts:** Is pairwise rejection acceptable for v1? Under it, "Destination = CA" and "Carrier = DHL_EXPRESS" cannot coexist even with a third rule covering CA + DHL_EXPRESS.
12. **BR-07 boundary cases:** Confirm the rows in the BR-07 table. In particular:
    - Destination = AU is currently a conflict with Destination = CA, AU. It is not treated as an exception, even though it lists fewer values.
    - Destination = AU, GB; Origin = CN is currently allowed over Destination = CA, AU.
13. **Deletion approval:** Is Admin sufficient to delete a rule whose effective window is in the past, or is Finance approval required (see #6, #7)?
14. **Draft cleanup:** Should Editors be able to discard their own drafts, given that drafts never affect billing?

---

## 19. Final Architecture Recommendation

Use a normalized, versioned **PostgreSQL billing configuration system** with an Engineering-owned UI and API.

Represent rule conditions as independent dimension/value records, allowing multiple values per dimension and treating absent dimensions as ALL.

Guarantee a single winning rule at publish time by allowing overlapping rules only where one is an exception of the other (BR-07).

Synchronize published configuration to Snowflake as validated snapshots.

Use a Data-owned generic matching engine that dynamically compares configuration condition keys against order attributes stored in a semi-structured `match_context`.

Keep OMS fee calculation and rerun mechanics under Engineering ownership while Data owns payer determination, fee waivers, billing checks, and immutable billing history.

**Design principle:** Configuration changes should be handled through data and UI, not recurring changes to billing SQL.
