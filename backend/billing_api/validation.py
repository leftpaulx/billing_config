"""Rule validation (spec §8.4, §9.3) and conflict analysis.

Precedence (replaces BR-07 ranking; there is no priority field):
  Two rules for the same merchant + fee type with overlapping effective windows
  may both match a fee line only if one is an *exception* of the other: it
  constrains every dimension the other constrains, plus at least one dimension
  the other leaves as "All". The exception (more constrained dimensions) wins.
  Any other overlap is a blocking conflict, e.g.
    DESTINATION_COUNTRY (CA, AU)  vs  DESTINATION_COUNTRY (AU, GB)   -> conflict on AU
    DESTINATION_COUNTRY (CA)      vs  no conditions (All)            -> OK, CA rule wins for CA
    DESTINATION_COUNTRY (CA)      vs  CARRIER (DHL_EXPRESS)          -> conflict (neither is narrower)
  Because every pair of rules that can co-match has strictly nested dimension
  sets, "most constrained dimensions wins" always yields exactly one winner in
  the Snowflake matcher.

`validate_rule` returns issues with severity:
  error   – blocks publishing
  warning – publishable, but needs a human look
  info    – what publishing will do (e.g. supersede a revision)
It also returns the supersession plan that `publish_rule` executes.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from psycopg import Connection

from .auth import User

ConditionMap = dict[str, set[str]]


@dataclass
class SupersedeAction:
    rule_id: str
    revision: int
    new_effective_to: datetime
    void: bool  # True: predecessor never takes effect (window collapses to empty)


@dataclass
class ValidationResult:
    issues: list[dict[str, Any]] = field(default_factory=list)
    supersede: list[SupersedeAction] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i["severity"] == "error" for i in self.issues)

    def add(self, severity: str, code: str, message: str, field: str | None = None, related_rule_id=None):
        self.issues.append({
            "severity": severity,
            "code": code,
            "message": message,
            "field": field,
            "related_rule_id": str(related_rule_id) if related_rule_id else None,
        })


def condition_map(conditions: list[dict[str, Any]]) -> ConditionMap:
    return {c["dimension"]: set(c["values"]) for c in conditions}


def can_match_same_fee_line(a: ConditionMap, b: ConditionMap) -> bool:
    """Two rules can both match one order iff every dimension they both constrain
    shares at least one value. A dimension constrained by only one rule never
    prevents overlap, because the other rule accepts ALL values there."""
    return all(a[d] & b[d] for d in a.keys() & b.keys())


def precedence(a: ConditionMap, b: ConditionMap) -> str | None:
    """For two rules that can co-match: 'a' or 'b' if that rule is an exception of
    the other (strict superset of constrained dimensions), otherwise None (conflict)."""
    if a.keys() > b.keys():
        return "a"
    if b.keys() > a.keys():
        return "b"
    return None


def overlap_region(a: ConditionMap, b: ConditionMap) -> ConditionMap:
    """The fee lines both rules match, per dimension (dimensions neither constrains are All)."""
    return {d: (a[d] & b[d]) if d in a and d in b else a.get(d) or b[d] for d in a.keys() | b.keys()}


def describe_conditions(conds: ConditionMap, names: dict[str, str] | None = None) -> str:
    if not conds:
        return "all orders"
    names = names or {}
    return "; ".join(f"{names.get(d, d)} {'/'.join(sorted(v))}" for d, v in sorted(conds.items()))


def describe_dims(dims, names: dict[str, str]) -> str:
    return ", ".join(names.get(d, d) for d in sorted(dims))


def rule_label(row: dict[str, Any]) -> str:
    return f"rule {str(row['logical_rule_id'])[:8]} rev {row['revision']}"


def validate_rule(conn: Connection, rule: dict[str, Any], user: User) -> ValidationResult:
    res = ValidationResult()
    now = conn.execute("SELECT now() AS now").fetchone()["now"]
    conds = condition_map(rule["conditions"])

    # 1. merchant / 2. fee type
    merchant = conn.execute(
        "SELECT is_active FROM ref_merchant WHERE merchant_id = %s", (rule["merchant_id"],)
    ).fetchone()
    if not merchant:
        res.add("error", "UNKNOWN_MERCHANT", f"Merchant {rule['merchant_id']} does not exist", "merchant_id")
    elif not merchant["is_active"]:
        res.add("error", "INACTIVE_MERCHANT", f"Merchant {rule['merchant_id']} is inactive", "merchant_id")

    fee = conn.execute("SELECT is_active FROM ref_fee_type WHERE fee_type = %s", (rule["fee_type"],)).fetchone()
    if not fee or not fee["is_active"]:
        res.add("error", "UNSUPPORTED_FEE_TYPE", f"Fee type {rule['fee_type']} is not supported", "fee_type")

    # 3/4/9. dimensions and values
    catalog = {
        r["dimension_key"]: r
        for r in conn.execute("SELECT * FROM billing_dimension_catalog").fetchall()
    }
    for dim, values in conds.items():
        entry = catalog.get(dim)
        f = f"conditions.{dim}"
        if not entry:
            res.add("error", "UNSUPPORTED_DIMENSION", f"Dimension {dim} is not registered", f)
            continue
        if not entry["is_active"]:
            res.add("error", "INACTIVE_DIMENSION", f"Dimension {entry['display_name']} is not active for new rules", f)
        if not entry["is_available_in_staging"]:
            res.add("error", "DIMENSION_NOT_STAGED",
                    f"{entry['display_name']} is not yet available in billing staging (match_context); "
                    "rules using it cannot be published until Data enables it", f)
        if not values:
            res.add("error", "EMPTY_DIMENSION", f"Specific {entry['display_name']} selected without any value", f)
        known = {
            r["dimension_value"]: r["is_active"]
            for r in conn.execute(
                "SELECT dimension_value, is_active FROM ref_dimension_value "
                "WHERE dimension_key = %s AND dimension_value = ANY(%s)",
                (dim, list(values)),
            ).fetchall()
        }
        unknown = sorted(v for v in values if v not in known)
        inactive = sorted(v for v, active in known.items() if not active)
        if unknown:
            res.add("error", "INVALID_DIMENSION_VALUE",
                    f"Unknown {entry['display_name']} value(s): {', '.join(unknown)}", f)
        if inactive:
            res.add("error", "INACTIVE_DIMENSION_VALUE",
                    f"Inactive {entry['display_name']} value(s): {', '.join(inactive)}", f)

    if not conds:
        res.add("info", "CATCH_ALL",
                f"No conditions: this is the default for every {rule['fee_type']} fee of {rule['merchant_id']}")

    # 6. payer
    payer_type, payer_ref = rule["payer_type"], rule["payer_reference"]
    if not payer_type:
        res.add("error", "MISSING_PAYER", "Payer type is required", "payer_type")
    elif payer_type == "PARTNER":
        if not payer_ref:
            res.add("error", "PARTNER_REFERENCE_REQUIRED", "Partner payer selected without a partner", "payer_reference")
        elif not conn.execute(
            "SELECT 1 FROM ref_partner WHERE partner_id = %s AND is_active", (payer_ref,)
        ).fetchone():
            res.add("error", "INVALID_PARTNER", f"Partner {payer_ref} does not exist or is inactive", "payer_reference")
    elif payer_type == "MERCHANT" and payer_ref:
        res.add("error", "UNEXPECTED_PAYER_REFERENCE",
                "MERCHANT payer resolves to the order's merchant; payer entity must be empty", "payer_reference")
    elif payer_type == "CARRIER" and payer_ref:
        if not conn.execute(
            "SELECT 1 FROM ref_dimension_value WHERE dimension_key = 'CARRIER' AND dimension_value = %s AND is_active",
            (payer_ref,),
        ).fetchone():
            res.add("error", "INVALID_CARRIER_PAYER", f"Carrier {payer_ref} is not a known carrier", "payer_reference")
        else:
            res.add("warning", "CARRIER_PAYER_OVERRIDE",
                    f"Carrier {payer_ref} will be billed instead of the carrier on the fee/order "
                    "(open design decision §18 #3)", "payer_reference")

    # 7. effective period
    eff_from, eff_to = rule["effective_from"], rule["effective_to"]
    if eff_from is None:
        res.add("error", "MISSING_EFFECTIVE_FROM", "Effective start is required", "effective_from")
        return res
    if eff_to is not None and eff_to <= eff_from:
        res.add("error", "END_BEFORE_START", "Effective end must be after effective start", "effective_to")
        return res
    if eff_from < now:
        if user.has("ADMIN"):
            res.add("warning", "RETROACTIVE",
                    "Effective start is in the past: this is a retroactive change. Finalized invoices are not "
                    "rewritten; affected periods need an explicit adjustment/rebilling assessment", "effective_from")
        else:
            res.add("error", "RETROACTIVE",
                    "Effective start is in the past; retroactive changes require Admin authorization", "effective_from")

    _check_revisions(conn, rule, res)
    _check_conflicts(conn, rule, conds, {k: v["display_name"] for k, v in catalog.items()}, res)
    return res


def _check_revisions(conn: Connection, rule: dict[str, Any], res: ValidationResult) -> None:
    """Published revisions of the same logical rule must not overlap. A successor
    revision supersedes overlapping predecessors: an earlier-starting one is
    end-dated, a later-starting one is voided. A successor that would leave a
    gap-filling tail of a predecessor is rejected."""
    eff_from, eff_to = rule["effective_from"], rule["effective_to"]
    overlapping = conn.execute(
        """
        SELECT rule_id, logical_rule_id, revision, status, effective_from, effective_to
        FROM billing_rule
        WHERE logical_rule_id = %s AND rule_id <> %s AND status IN ('PUBLISHED', 'RETIRED')
          AND tstzrange(effective_from, effective_to, '[)') && tstzrange(%s, %s, '[)')
        ORDER BY revision
        """,
        (rule["logical_rule_id"], rule["rule_id"], eff_from, eff_to),
    ).fetchall()
    for p in overlapping:
        if p["status"] == "RETIRED":
            res.add("error", "OVERLAPPING_REVISION",
                    f"Overlaps the historical window of retired revision {p['revision']} "
                    f"(ends {p['effective_to'].isoformat()}); start on or after that date", "effective_from", p["rule_id"])
        elif eff_to is not None and (p["effective_to"] is None or p["effective_to"] > eff_to):
            res.add("error", "OVERLAPPING_REVISION",
                    f"Revision {p['revision']} remains effective after this revision ends; the same logical rule "
                    "cannot have overlapping effective versions", "effective_to", p["rule_id"])
        elif p["effective_from"] < eff_from:
            res.supersede.append(SupersedeAction(str(p["rule_id"]), p["revision"], eff_from, void=False))
            res.add("info", "WILL_END_DATE",
                    f"Publishing will end revision {p['revision']} at {eff_from.isoformat()}", None, p["rule_id"])
        else:
            res.supersede.append(SupersedeAction(str(p["rule_id"]), p["revision"], p["effective_from"], void=True))
            res.add("info", "WILL_VOID",
                    f"Revision {p['revision']} has not taken effect yet and will be fully superseded", None, p["rule_id"])


def _check_conflicts(conn: Connection, rule: dict[str, Any], conds: ConditionMap,
                     names: dict[str, str], res: ValidationResult) -> None:
    """Compare against other published rules for the same merchant/fee type whose
    effective windows overlap (see module docstring for the precedence policy)."""
    others = conn.execute(
        """
        SELECT r.rule_id, r.logical_rule_id, r.revision,
               coalesce(json_object_agg(c.dimension_key, c.vals) FILTER (WHERE c.dimension_key IS NOT NULL), '{}') AS conds
        FROM billing_rule r
        LEFT JOIN LATERAL (
            SELECT dimension_key, array_agg(dimension_value) AS vals
            FROM billing_rule_condition WHERE rule_id = r.rule_id GROUP BY dimension_key
        ) c ON TRUE
        WHERE r.merchant_id = %s AND r.fee_type = %s AND r.logical_rule_id <> %s
          AND r.status IN ('PUBLISHED', 'RETIRED')
          AND tstzrange(r.effective_from, r.effective_to, '[)') && tstzrange(%s, %s, '[)')
        GROUP BY r.rule_id
        """,
        (rule["merchant_id"], rule["fee_type"], rule["logical_rule_id"], rule["effective_from"], rule["effective_to"]),
    ).fetchall()

    for o in others:
        theirs = {k: set(v) for k, v in o["conds"].items()}
        if not can_match_same_fee_line(conds, theirs):
            continue
        label = f"{rule_label(o)} ({describe_conditions(theirs, names)})"
        region = describe_conditions(overlap_region(conds, theirs), names)
        winner = precedence(conds, theirs)
        if winner is None and conds.keys() == theirs.keys():
            shared = {d: conds[d] & theirs[d] for d in conds}
            res.add("error", "CONFLICTING_RULE",
                    f"Conflicts with {label}: both rules specify the same value(s) "
                    f"({describe_conditions(shared, names)}). A value can be specified by only one rule per "
                    "merchant and fee type; remove the overlapping values from one of the rules.",
                    "conditions", o["rule_id"])
        elif winner is None:
            res.add("error", "CONFLICTING_RULE",
                    f"Conflicts with {label}: both rules apply to {region}, and neither is an exception of the "
                    f"other (this rule specifies {describe_dims(conds.keys() - theirs.keys(), names)}; the other "
                    f"specifies {describe_dims(theirs.keys() - conds.keys(), names)}). Remove the overlap, or make "
                    "one rule also specify the other's dimensions.",
                    "conditions", o["rule_id"])
        elif winner == "a":
            res.add("info", "OVERRIDES_RULE",
                    f"Exception of {label}: for {region} this rule applies, because it also specifies "
                    f"{describe_dims(conds.keys() - theirs.keys(), names)}", "conditions", o["rule_id"])
        else:
            res.add("info", "OVERRIDDEN_BY_RULE",
                    f"{label} is an exception of this rule: for {region} that rule applies, because it also "
                    f"specifies {describe_dims(theirs.keys() - conds.keys(), names)}", "conditions", o["rule_id"])
