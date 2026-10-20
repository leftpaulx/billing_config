"""Rule persistence and lifecycle: draft CRUD, publish (§9.4), retire, delete, clone, history."""

import json
from datetime import datetime, time, timedelta, timezone
from typing import Any

from psycopg import Connection, errors
from psycopg.types.json import Jsonb

from .auth import User
from .errors import ApiError, not_found
from .schemas import ConditionIn, RuleCreate, RuleUpdate
from .validation import ValidationResult, validate_rule

RULE_SELECT = """
SELECT r.rule_id, r.logical_rule_id, r.revision,
       r.merchant_id, m.display_name AS merchant_name,
       r.fee_type, f.display_name AS fee_type_name,
       r.payer_type, r.payer_reference, r.is_waived,
       r.effective_from, r.effective_to, r.status, r.change_reason,
       r.created_at, r.created_by, r.updated_at, r.updated_by,
       r.published_at, r.published_by, r.retired_at, r.retired_by,
       r.deleted_at, r.deleted_by, r.row_version,
       coalesce((
           SELECT json_agg(json_build_object('dimension', g.dimension_key, 'operator', 'IN', 'values', g.vals)
                           ORDER BY g.sort_order, g.dimension_key)
           FROM (
               SELECT c.dimension_key, d.sort_order, array_agg(c.dimension_value ORDER BY c.dimension_value) AS vals
               FROM billing_rule_condition c
               JOIN billing_dimension_catalog d USING (dimension_key)
               WHERE c.rule_id = r.rule_id
               GROUP BY c.dimension_key, d.sort_order
           ) g
       ), '[]'::json) AS conditions
FROM billing_rule r
JOIN ref_merchant m USING (merchant_id)
JOIN ref_fee_type f USING (fee_type)
"""

BUSINESS_FIELDS = (
    "merchant_id", "fee_type", "payer_type", "payer_reference", "is_waived",
    "effective_from", "effective_to", "change_reason",
)


# ----------------------------------------------------------------------------- reads

def get_rule(conn: Connection, rule_id: str, *, lock: bool = False) -> dict[str, Any]:
    if lock:
        # Lock the base row first; RULE_SELECT has aggregates so it cannot use FOR UPDATE.
        if not conn.execute("SELECT 1 FROM billing_rule WHERE rule_id = %s FOR UPDATE", (rule_id,)).fetchone():
            raise not_found("Rule")
    row = conn.execute(RULE_SELECT + " WHERE r.rule_id = %s", (rule_id,)).fetchone()
    if not row:
        raise not_found("Rule")
    return row


def get_rule_detail(conn: Connection, rule_id: str) -> dict[str, Any]:
    rule = get_rule(conn, rule_id)
    rule["revisions"] = conn.execute(
        """
        SELECT rule_id, revision, status, effective_from, effective_to, is_waived, payer_type,
               published_at, retired_at, deleted_at
        FROM billing_rule WHERE logical_rule_id = %s ORDER BY revision DESC
        """,
        (rule["logical_rule_id"],),
    ).fetchall()
    return rule


def list_rules(
    conn: Connection,
    *,
    q: str | None = None,
    merchant_id: str | None = None,
    fee_type: str | None = None,
    statuses: list[str] | None = None,
    effective_on: datetime | None = None,
    dimension: str | None = None,
    value: str | None = None,
    include_ended: bool = False,
    limit: int = 500,
) -> list[dict[str, Any]]:
    where, params = [], {}
    if q:
        where.append("""(r.rule_id::text ILIKE %(q_prefix)s OR r.logical_rule_id::text ILIKE %(q_prefix)s
                         OR r.merchant_id ILIKE %(q_any)s OR m.display_name ILIKE %(q_any)s
                         OR r.change_reason ILIKE %(q_any)s)""")
        params |= {"q_prefix": f"{q.strip()}%", "q_any": f"%{q.strip()}%"}
    if merchant_id:
        where.append("r.merchant_id = %(merchant_id)s")
        params["merchant_id"] = merchant_id
    if fee_type:
        where.append("r.fee_type = %(fee_type)s")
        params["fee_type"] = fee_type
    if statuses:
        where.append("r.status = ANY(%(statuses)s)")
        params["statuses"] = [s.upper() for s in statuses]
    else:
        where.append("r.status <> 'DELETED'")
    if effective_on:
        where.append("tstzrange(r.effective_from, r.effective_to, '[)') @> %(effective_on)s::timestamptz")
        params["effective_on"] = effective_on
    if dimension and value:
        # "Applicable" = explicitly lists the value OR does not constrain the dimension (ALL).
        where.append("""(
            NOT EXISTS (SELECT 1 FROM billing_rule_condition c
                        WHERE c.rule_id = r.rule_id AND c.dimension_key = %(dim)s)
            OR EXISTS (SELECT 1 FROM billing_rule_condition c
                       WHERE c.rule_id = r.rule_id AND c.dimension_key = %(dim)s AND c.dimension_value = %(val)s))""")
        params |= {"dim": dimension.upper(), "val": value.upper()}
    if not include_ended:
        where.append("NOT (r.status <> 'DRAFT' AND r.effective_to IS NOT NULL AND r.effective_to <= now())")
    sql = RULE_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.merchant_id, r.fee_type, r.effective_from NULLS LAST, r.logical_rule_id, r.revision DESC LIMIT %(limit)s"
    params["limit"] = limit
    return conn.execute(sql, params).fetchall()


def get_history(conn: Connection, rule_id: str) -> list[dict[str, Any]]:
    rule = get_rule(conn, rule_id)
    return conn.execute(
        """
        SELECT a.audit_id, a.rule_id, r.revision, a.action, a.actor, a.occurred_at,
               a.previous_value, a.new_value, a.reason
        FROM billing_rule_audit a
        LEFT JOIN billing_rule r ON r.rule_id = a.rule_id
        WHERE a.logical_rule_id = %s
        ORDER BY a.occurred_at DESC, a.audit_id DESC
        """,
        (rule["logical_rule_id"],),
    ).fetchall()


# ----------------------------------------------------------------------------- helpers

def _snapshot(rule: dict[str, Any]) -> Jsonb:
    keys = ("rule_id", "logical_rule_id", "revision", "status", *BUSINESS_FIELDS, "conditions")
    return Jsonb(json.loads(json.dumps({k: rule.get(k) for k in keys}, default=str)))


def _audit(conn: Connection, rule: dict[str, Any], action: str, actor: str, *,
           previous: dict | None = None, new: dict | None = None, reason: str | None = None) -> None:
    conn.execute(
        """INSERT INTO billing_rule_audit (rule_id, logical_rule_id, action, actor, previous_value, new_value, reason)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (rule["rule_id"], rule["logical_rule_id"], action, actor,
         _snapshot(previous) if previous else None, _snapshot(new) if new else None, reason),
    )


def canonicalize_conditions(conditions: list[ConditionIn]) -> list[tuple[str, list[str]]]:
    """Normalize keys/values and reject shapes that would silently change meaning.
    In particular an empty value list must not be stored: zero rows means ALL."""
    seen_dims: set[str] = set()
    out = []
    for c in conditions:
        dim = c.dimension.strip().upper()
        if dim in seen_dims:
            raise ApiError(422, "DUPLICATE_DIMENSION", f"Dimension {dim} appears more than once")
        seen_dims.add(dim)
        values = [v.strip().upper() for v in c.values]
        if not values or any(not v for v in values):
            raise ApiError(422, "EMPTY_DIMENSION",
                           f"{dim}: Specific scope requires at least one non-empty value (omit the dimension for ALL)")
        dupes = sorted({v for v in values if values.count(v) > 1})
        if dupes:
            raise ApiError(422, "DUPLICATE_VALUES", f"{dim}: duplicate values {', '.join(dupes)}")
        out.append((dim, values))
    return out


def _write_conditions(conn: Connection, rule_id: str, conditions: list[tuple[str, list[str]]]) -> None:
    conn.execute("DELETE FROM billing_rule_condition WHERE rule_id = %s", (rule_id,))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO billing_rule_condition (rule_id, dimension_key, dimension_value) VALUES (%s, %s, %s)",
            [(rule_id, dim, v) for dim, values in conditions for v in values],
        )


def _translate_db_error(exc: Exception) -> ApiError:
    msg = str(exc).splitlines()[0]
    if isinstance(exc, errors.ForeignKeyViolation):
        detail = getattr(exc.diag, "message_detail", None) or msg
        return ApiError(422, "UNKNOWN_REFERENCE", f"Unknown reference value: {detail}")
    if isinstance(exc, errors.UniqueViolation) and "ux_billing_rule_one_draft" in msg:
        return ApiError(409, "DRAFT_EXISTS", "This rule already has an open draft revision")
    if isinstance(exc, errors.CheckViolation):
        return ApiError(422, "CONSTRAINT_VIOLATION", msg)
    if isinstance(exc, (errors.IntegrityConstraintViolation,)):
        return ApiError(409, "INTEGRITY_VIOLATION", msg)
    raise exc


def _check_row_version(rule: dict[str, Any], expected: int) -> None:
    if rule["row_version"] != expected:
        raise ApiError(409, "STALE_ROW_VERSION",
                       f"Rule was modified by someone else (row_version {rule['row_version']}, you sent {expected}). "
                       "Reload and retry.")


def _next_midnight_utc(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(timezone.utc).date() + timedelta(days=1), time(0), tzinfo=timezone.utc)


# ----------------------------------------------------------------------------- writes

def create_draft(conn: Connection, body: RuleCreate, user: User) -> dict[str, Any]:
    conditions = canonicalize_conditions(body.conditions)
    try:
        with conn.transaction():
            rule_id = conn.execute(
                """
                INSERT INTO billing_rule (merchant_id, fee_type, payer_type, payer_reference, is_waived,
                                          effective_from, effective_to, change_reason, created_by, updated_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING rule_id
                """,
                (body.merchant_id, body.fee_type, body.payer_type, body.payer_reference or None, body.is_waived,
                 body.effective_from, body.effective_to, body.change_reason, user.id, user.id),
            ).fetchone()["rule_id"]
            _write_conditions(conn, rule_id, conditions)
            rule = get_rule(conn, rule_id)
            _audit(conn, rule, "CREATED", user.id, new=rule, reason=body.change_reason)
    except errors.Error as exc:
        raise _translate_db_error(exc) from exc
    return rule


def update_draft(conn: Connection, rule_id: str, body: RuleUpdate, user: User) -> dict[str, Any]:
    fields = body.model_fields_set - {"row_version", "conditions"}
    try:
        with conn.transaction():
            before = get_rule(conn, rule_id, lock=True)
            if before["status"] != "DRAFT":
                raise ApiError(409, "NOT_A_DRAFT",
                               f"Rule is {before['status']}; only drafts can be edited. Duplicate it as a new revision.")
            _check_row_version(before, body.row_version)
            for required in ("merchant_id", "fee_type", "is_waived"):
                if required in fields and getattr(body, required) is None:
                    raise ApiError(422, "REQUIRED_FIELD", f"{required} cannot be null")

            values = {f: getattr(body, f) for f in fields}
            if "payer_reference" in values:
                values["payer_reference"] = values["payer_reference"] or None
            assignments = ", ".join(f"{f} = %({f})s" for f in values)
            conn.execute(
                f"UPDATE billing_rule SET {assignments + ', ' if assignments else ''}updated_by = %(user)s "
                "WHERE rule_id = %(rule_id)s",
                values | {"user": user.id, "rule_id": rule_id},
            )
            if body.conditions is not None:
                _write_conditions(conn, rule_id, canonicalize_conditions(body.conditions))
            after = get_rule(conn, rule_id)
            _audit(conn, after, "UPDATED", user.id, previous=before, new=after, reason=after["change_reason"])
    except errors.Error as exc:
        raise _translate_db_error(exc) from exc
    return after


def delete_rule(conn: Connection, rule_id: str, row_version: int, reason: str, user: User) -> list[str]:
    """Admin-only soft delete. A draft deletes just that draft; a published or
    retired version deletes every version of its logical rule, so no partial
    history (e.g. an end-dated predecessor) is left behind. Rows stay for audit
    and for Snowflake snapshots that reference rule_id, but DELETED rules never
    sync, never match and never take part in conflict checks again."""
    if not reason or not reason.strip():
        raise ApiError(422, "REASON_REQUIRED", "A reason is required to delete a rule")
    try:
        with conn.transaction():
            rule = get_rule(conn, rule_id, lock=True)
            if rule["status"] == "DELETED":
                raise ApiError(409, "ALREADY_DELETED", "Rule is already deleted")
            _check_row_version(rule, row_version)
            if rule["status"] == "DRAFT":
                targets = [rule_id]
            else:
                targets = [str(r["rule_id"]) for r in conn.execute(
                    """SELECT rule_id FROM billing_rule
                       WHERE logical_rule_id = %s AND status <> 'DELETED' ORDER BY revision FOR UPDATE""",
                    (rule["logical_rule_id"],),
                ).fetchall()]
            for target in targets:
                before = get_rule(conn, target)
                conn.execute(
                    """UPDATE billing_rule SET status = 'DELETED', deleted_at = now(), deleted_by = %s,
                           updated_by = %s WHERE rule_id = %s""",
                    (user.id, user.id, target),
                )
                _audit(conn, before, "DELETED", user.id, previous=before, new=get_rule(conn, target), reason=reason)
    except errors.Error as exc:
        raise _translate_db_error(exc) from exc
    return targets


def validate(conn: Connection, rule_id: str, user: User) -> ValidationResult:
    return validate_rule(conn, get_rule(conn, rule_id), user)


def publish_rule(conn: Connection, rule_id: str, row_version: int, reason: str | None, user: User) -> dict[str, Any]:
    """Spec §9.4: lock, verify row_version, validate, supersede predecessors,
    transition to PUBLISHED and audit — atomically."""
    rejected: ValidationResult | None = None
    try:
        with conn.transaction():
            rule = get_rule(conn, rule_id, lock=True)
            if rule["status"] != "DRAFT":
                raise ApiError(409, "NOT_A_DRAFT", f"Rule is already {rule['status']}")
            _check_row_version(rule, row_version)
            # Serialise publishes for one merchant/fee type so two concurrent
            # publishes cannot each pass the conflict check against stale data.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                         (f"{rule['merchant_id']}|{rule['fee_type']}",))

            result = validate_rule(conn, rule, user)
            if not result.ok:
                rejected = result
                raise ApiError(422, "VALIDATION_FAILED", "Rule has blocking validation errors", result.issues)

            for action in result.supersede:
                prev = get_rule(conn, action.rule_id, lock=True)
                if action.void:
                    conn.execute(
                        """UPDATE billing_rule SET status = 'RETIRED', effective_to = effective_from,
                               retired_at = now(), retired_by = %s, updated_by = %s WHERE rule_id = %s""",
                        (user.id, user.id, action.rule_id),
                    )
                else:
                    conn.execute(
                        "UPDATE billing_rule SET effective_to = %s, updated_by = %s WHERE rule_id = %s",
                        (action.new_effective_to, user.id, action.rule_id),
                    )
                _audit(conn, prev, "SUPERSEDED", user.id, previous=prev, new=get_rule(conn, action.rule_id),
                       reason=f"Superseded by revision {rule['revision']}")

            conn.execute(
                """UPDATE billing_rule SET status = 'PUBLISHED', published_at = now(), published_by = %s,
                       updated_by = %s WHERE rule_id = %s""",
                (user.id, user.id, rule_id),
            )
            published = get_rule(conn, rule_id)
            _audit(conn, published, "PUBLISHED", user.id, previous=rule, new=published,
                   reason=reason or rule["change_reason"])
    except ApiError:
        if rejected is not None:
            # Record the rejected attempt outside the rolled-back transaction.
            with conn.transaction():
                conn.execute(
                    """INSERT INTO billing_rule_audit (rule_id, logical_rule_id, action, actor, new_value, reason)
                       VALUES (%s, %s, 'REJECTED', %s, %s, %s)""",
                    (rule["rule_id"], rule["logical_rule_id"], user.id,
                     Jsonb({"issues": [i for i in rejected.issues if i["severity"] == "error"]}), reason),
                )
        raise
    except errors.Error as exc:
        raise _translate_db_error(exc) from exc
    published["validation"] = result.issues
    return published


def retire_rule(conn: Connection, rule_id: str, row_version: int, effective_at: datetime | None,
                reason: str, user: User) -> dict[str, Any]:
    if not reason or not reason.strip():
        raise ApiError(422, "REASON_REQUIRED", "A reason is required to retire a rule")
    try:
        with conn.transaction():
            rule = get_rule(conn, rule_id, lock=True)
            if rule["status"] != "PUBLISHED":
                raise ApiError(409, "NOT_PUBLISHED", f"Only published rules can be retired (rule is {rule['status']})")
            _check_row_version(rule, row_version)
            now = conn.execute("SELECT now() AS now").fetchone()["now"]
            retire_at = effective_at or now
            if retire_at < now and not user.has("ADMIN"):
                raise ApiError(403, "RETROACTIVE", "Retiring from a past timestamp requires Admin authorization")
            if rule["effective_to"] is not None and retire_at >= rule["effective_to"]:
                raise ApiError(422, "ALREADY_ENDS",
                               f"Rule already ends at {rule['effective_to'].isoformat()}, before the retirement time")
            new_to = max(retire_at, rule["effective_from"])  # retiring before start => empty window
            conn.execute(
                """UPDATE billing_rule SET status = 'RETIRED', effective_to = %s, retired_at = now(),
                       retired_by = %s, updated_by = %s WHERE rule_id = %s""",
                (new_to, user.id, user.id, rule_id),
            )
            retired = get_rule(conn, rule_id)
            _audit(conn, retired, "RETIRED", user.id, previous=rule, new=retired, reason=reason)
    except errors.Error as exc:
        raise _translate_db_error(exc) from exc
    return retired


def clone_rule(conn: Connection, rule_id: str, as_new_rule: bool, user: User) -> dict[str, Any]:
    try:
        with conn.transaction():
            src = get_rule(conn, rule_id)
            if not as_new_rule and src["status"] == "DRAFT":
                raise ApiError(409, "NOT_PUBLISHED", "A draft cannot be the source of a new revision; edit it instead")
            if not as_new_rule and src["status"] == "DELETED":
                raise ApiError(409, "DELETED", "A deleted rule cannot get new revisions; duplicate it as a new rule")
            now = conn.execute("SELECT now() AS now").fetchone()["now"]
            start = _next_midnight_utc(now)
            if src["effective_from"] and src["effective_from"] > start:
                start = src["effective_from"]
            if as_new_rule:
                logical, revision = None, 1
            else:
                logical = src["logical_rule_id"]
                revision = conn.execute(
                    "SELECT max(revision) + 1 AS n FROM billing_rule WHERE logical_rule_id = %s", (logical,)
                ).fetchone()["n"]
            new_id = conn.execute(
                """
                INSERT INTO billing_rule (logical_rule_id, revision, merchant_id, fee_type, payer_type, payer_reference,
                                          is_waived, effective_from, effective_to, change_reason,
                                          created_by, updated_by)
                VALUES (coalesce(%s, gen_random_uuid()), %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s, %s)
                RETURNING rule_id
                """,
                (logical, revision, src["merchant_id"], src["fee_type"], src["payer_type"], src["payer_reference"],
                 src["is_waived"], start, user.id, user.id),
            ).fetchone()["rule_id"]
            _write_conditions(conn, new_id, [(c["dimension"], c["values"]) for c in src["conditions"]])
            rule = get_rule(conn, new_id)
            note = "Duplicated as new rule" if as_new_rule else f"New revision of revision {src['revision']}"
            _audit(conn, rule, "CREATED", user.id, new=rule, reason=f"{note} (source {src['rule_id']})")
    except errors.Error as exc:
        raise _translate_db_error(exc) from exc
    return rule
