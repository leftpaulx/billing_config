# Billing Configuration POC

Proof of concept for the *OpenBorder — Billing Configuration System* tech spec (Draft v1.0). It covers
the Engineering-owned write path: **PostgreSQL schema → configuration API → rule editor UI**, plus
the read-only views that the Data-owned Snowflake sync will consume.

```
frontend/index.html  ──►  FastAPI (backend/billing_api)  ──►  PostgreSQL (db/*.sql)  ──►  v_sync_* views ─► Prefect ─► Snowflake
     rule list/editor        validation, publish txn,            versioned rules,             (Data-owned, not in this POC)
                             conflicts, audit, roles              triggers, audit
```

## Run it

Requires [uv](https://docs.astral.sh/uv/). No Postgres install is needed: the dev script starts an
embedded PostgreSQL 16 (`pgserver`) in `./.pgdata`.

```bash
uv sync
uv run scripts/dev.py            # http://localhost:8000
uv run scripts/dev.py --reset    # rebuild schema + seed data
uv run pytest                    # 22 API/DB tests, each on a fresh cloned database
```

To use a real server instead: `docker compose up -d`, then
`DATABASE_URL=postgresql://billing:billing@localhost:5432/billing uv run scripts/dev.py`.

Use the **Role** switcher in the top bar to try Viewer / Editor / Publisher / Admin.

## What's in the box

| Path | Spec | Contents |
|---|---|---|
| `db/001_schema.sql` | §6, §13, §15 | `billing_rule`, `billing_rule_condition`, `billing_dimension_catalog`, `billing_rule_audit`, reference tables, indexes, integrity triggers |
| `db/002_seed.sql` | §7 | Reference values, dimension catalog, R101/R102 from the spec, a superseded revision, a retired rule, an incomplete draft |
| `db/003_sync_views.sql` | §5.2, §10, §14 | `v_sync_billing_rule`, `v_sync_billing_rule_condition`, read-only `billing_sync_ro` role |
| `backend/billing_api/` | §9 | REST API: all §9.1 endpoints, plus discard-draft and reference lookups |
| `backend/billing_api/validation.py` | §8.4, §9.3, BR-07 | Validation, conflict/exception policy, supersession planning |
| `frontend/index.html` | §8 | Rule list with filters, three-section editor, live summary, validation panel, revisions, change history, retire |
| `tests/test_api.py` | §16 | Acceptance-style tests |

### Enforced in the database, not just in the API

- Published and retired business fields are immutable. `effective_to` can only be shortened (by supersession or retirement).
- Status can only move DRAFT → PUBLISHED → RETIRED, and any status → DELETED (terminal). Published versions cannot be hard-deleted or truncated, and deleting cannot change a version's effective window.
- Conditions can only change while their rule is a DRAFT.
- Published revisions of one logical rule cannot have overlapping windows. This is a constraint trigger guarded by an advisory lock.
- At most one open draft per logical rule.
- Every UPDATE bumps `row_version` automatically, which gives the API its optimistic locking.
- `billing_rule_audit` is append-only (UPDATE, DELETE and TRUNCATE are all blocked).

### Validation (Validate button / publish)

Validation results come in three severities: **error** (blocks publishing), **warning** and **info**.

- **Reference checks:** merchant, fee type, dimension registered, active and staged (`is_available_in_staging`), canonical values.
- **Payer rules:** PARTNER needs a partner. MERCHANT must have no payer entity. CARRIER with an explicit carrier raises a warning (open decision §18 #3).
- **Dates:** end after start. A start in the past is a retroactive change: blocking for non-Admins, a warning for Admins.
- **Conflicts (no priority field):** this replaces the BR-07 ranking. Compare two published rules for the same merchant and fee type whose effective windows overlap and that can match the same fee line:
  - **Exception (info, allowed):** one rule specifies every dimension the other specifies, plus at least one the other leaves as "All". The exception wins where both match. Example: `Destination CA` (waived) over a rule with no conditions (carrier pays). The spec's R102 over R101 is also an exception.
  - **Same values in the same dimensions (blocking):** for example `Destination CA, AU` vs `Destination AU, GB` (overlap on AU). `Destination CA, AU` vs `Destination AU` is also rejected.
  - **Different dimensions with neither an exception (blocking):** for example `Destination CA` vs `Carrier DHL`. Nothing decides a CA + DHL fee line without priority.
  - **Never co-match (allowed):** rules that can never match the same fee line can coexist, for example `CA + DHL` vs `CA + FedEx` (BR-03 pairings).

  Because co-matching rules always have strictly nested dimension sets, the Snowflake matcher can rank by "most constrained dimensions wins" and is guaranteed exactly one winner.
- **Supersession:** publishing a successor revision ends an earlier-starting predecessor at the successor's start, and voids a predecessor that has not taken effect yet. All of this happens in the same transaction, together with the audit rows.

## POC decisions to confirm before production

1. **Auth is a stub.** Identity comes from the `X-User`/`X-Role` headers (`backend/billing_api/auth.py`). Swap `current_user` for SSO/JWT; nothing else changes.
2. **Reference data is local.** Merchants, partners and dimension values sit in `ref_*` tables, standing in for the OMS/reference-data sources (open decision §18 #9).
3. **RETIRED keeps its history.** Retiring sets `effective_to` and status RETIRED. The sync view still exports the historical window, so late billing events (BR-06) resolve correctly. A revision retired before its start gets an empty window and is excluded from the sync.
4. **Retroactive changes need Admin.** This applies to effective starts and retirements in the past. It implements §13 "explicit authorization". The impact assessment remains a process step.
5. **Conflict detection is pairwise.** `Destination CA` and `Carrier DHL` are rejected together, even if a third `CA + DHL` rule would decide the overlap. To express that case, list the other countries explicitly on the carrier rule. A `NOT IN` operator would make this simpler; the spec has it in phase 3.
8. **Delete is Admin-only and soft.** Deleting a draft removes just that draft. Deleting a published or retired version removes **every** version of that logical rule, so no end-dated predecessor is left behind. A reason is required. Rows get status `DELETED`. They are hidden from the list (except under the Deleted filter) and excluded from the sync view and from conflict checks. Rows and audit are kept, because §6.4 forbids hard-deleting published versions and Snowflake snapshots and ledger lines reference `rule_id`. Unlike Retire, delete also removes the rule's past window from future billing runs. Finalized invoices are unaffected. Non-Admins can no longer discard their own drafts.
6. **Drafts may be incomplete or invalid** (missing payer, end before start). The database enforces completeness once a rule leaves DRAFT.
7. **No overlap exclusion constraint.** A trigger is used instead of `btree_gist`, so the schema runs on any managed Postgres without extensions.

## Not in this POC

- The Snowflake sync (Prefect), snapshot manifest, dbt matching model and billing ledger. These are Data-owned (§10, §11). The `v_sync_*` views are the hand-off point. Read them inside one `REPEATABLE READ READ ONLY` transaction.
- Rule preview against example orders. The spec puts this in phase 2, and it must call the Data-owned matcher (§8.5).
