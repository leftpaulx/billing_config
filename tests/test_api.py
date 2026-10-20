from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from conftest import ADMIN, EDITOR, PUBLISHER, VIEWER

CATCH_ALL_DUTIES = "c0de0100-0000-4000-8000-000000000000"
R101 = "c0de0101-0000-4000-8000-000000000000"
R102 = "c0de0102-0000-4000-8000-000000000000"
SHIPPING_TWO_REVS = "c0de0103-0000-4000-8000-000000000000"
M789_DRAFT = "c0de0107-0000-4000-8000-000000000000"


def future(days: int) -> str:
    base = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (base + timedelta(days=days)).isoformat()


def rule_by_logical(client, logical_id: str, revision: int = 1) -> dict:
    rows = client.get("/billing/rules", params={"q": logical_id, "include_ended": True}, headers=VIEWER).json()
    return next(r for r in rows if r["logical_rule_id"] == logical_id and r["revision"] == revision)


def create(client, headers=EDITOR, **overrides) -> dict:
    body = {
        "merchant_id": "M789",
        "fee_type": "duties",
        "payer_type": "MERCHANT",
        "is_waived": False,
        "effective_from": future(30),
        "conditions": [{"dimension": "DESTINATION_COUNTRY", "operator": "IN", "values": ["US"]}],
        "change_reason": "test",
    } | overrides
    resp = client.post("/billing/rules", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def publish(client, rule, headers=PUBLISHER):
    return client.post(f"/billing/rules/{rule['rule_id']}/publish",
                       json={"row_version": rule["row_version"]}, headers=headers)


def delete(client, rule, headers=ADMIN, reason="created in error"):
    return client.request("DELETE", f"/billing/rules/{rule['rule_id']}",
                          json={"row_version": rule["row_version"], "reason": reason}, headers=headers)


def codes(issues, severity=None):
    return {i["code"] for i in issues if severity is None or i["severity"] == severity}


def test_seed_rules_are_listed_with_grouped_conditions(client):
    r101 = rule_by_logical(client, R101)
    assert r101["is_waived"] is True
    assert {c["dimension"]: c["values"] for c in r101["conditions"]} == {
        "DESTINATION_COUNTRY": ["CA", "MX", "US"],
        "CARRIER": ["DHL_EXPRESS", "FEDEX"],
    }


def test_create_validate_publish(client):
    rule = create(client)
    assert rule["status"] == "DRAFT"
    v = client.post(f"/billing/rules/{rule['rule_id']}/validate", headers=EDITOR).json()
    assert v["ok"], v
    resp = publish(client, rule)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "PUBLISHED"
    actions = [h["action"] for h in client.get(f"/billing/rules/{rule['rule_id']}/history", headers=VIEWER).json()]
    assert actions == ["PUBLISHED", "CREATED"]


def test_roles_are_enforced(client):
    rule = create(client)
    assert publish(client, rule, headers=EDITOR).status_code == 403
    assert client.post("/billing/rules", json={"merchant_id": "M123", "fee_type": "duties"},
                       headers=VIEWER).status_code == 403
    for headers in (EDITOR, PUBLISHER):
        assert delete(client, rule, headers=headers).status_code == 403


def dest(*values):
    return {"dimension": "DESTINATION_COUNTRY", "values": list(values)}


def carrier(*values):
    return {"dimension": "CARRIER", "values": list(values)}


def test_same_dimension_overlap_is_rejected(client):
    first = create(client, fee_type="shipping_fee", conditions=[dest("CA", "AU")])
    assert publish(client, first).status_code == 200

    second = create(client, fee_type="shipping_fee", conditions=[dest("AU", "GB")])
    resp = publish(client, second)
    assert resp.status_code == 422
    conflict = next(i for i in resp.json()["error"]["issues"] if i["code"] == "CONFLICTING_RULE")
    assert conflict["related_rule_id"] == first["rule_id"] and "AU" in conflict["message"]
    history = client.get(f"/billing/rules/{second['rule_id']}/history", headers=VIEWER).json()
    assert history[0]["action"] == "REJECTED"

    # A subset of the values is still the same dimension specified twice.
    narrower = client.patch(f"/billing/rules/{second['rule_id']}",
                            json={"row_version": second["row_version"], "conditions": [dest("AU")]}, headers=EDITOR).json()
    assert "CONFLICTING_RULE" in codes(client.post(f"/billing/rules/{narrower['rule_id']}/validate",
                                                   headers=EDITOR).json()["issues"], "error")

    disjoint = client.patch(f"/billing/rules/{second['rule_id']}",
                            json={"row_version": narrower["row_version"], "conditions": [dest("GB")]}, headers=EDITOR).json()
    assert publish(client, disjoint).status_code == 200


def test_all_does_not_conflict_with_specific(client):
    waive_ca = create(client, fee_type="shipping_fee", is_waived=True, conditions=[dest("CA")])
    assert publish(client, waive_ca).status_code == 200
    carrier_pays = create(client, fee_type="shipping_fee", payer_type="CARRIER", conditions=[])
    resp = publish(client, carrier_pays)
    assert resp.status_code == 200, resp.text
    assert "OVERRIDDEN_BY_RULE" in codes(resp.json()["validation"], "info")


def test_rules_that_never_match_the_same_fee_line_can_coexist(client):
    # BR-03: country/carrier pairings are separate rules.
    a = create(client, fee_type="shipping_fee", conditions=[dest("CA"), carrier("DHL_EXPRESS")])
    b = create(client, fee_type="shipping_fee", conditions=[dest("CA"), carrier("FEDEX")])
    assert publish(client, a).status_code == 200
    assert publish(client, b).status_code == 200


def test_different_dimensions_without_an_exception_conflict(client):
    # Overlaps R101 on CA (+ any carrier/FC): neither rule is an exception of the other.
    rule = create(client, merchant_id="M123", effective_from=future(60), conditions=[
        dest("CA"), {"dimension": "FC_CODE", "values": ["HKG01"]},
    ])
    v = client.post(f"/billing/rules/{rule['rule_id']}/validate", headers=EDITOR).json()
    by_rule = {i["related_rule_id"]: i for i in v["issues"] if i["related_rule_id"]}
    assert by_rule[rule_by_logical(client, R101)["rule_id"]]["code"] == "CONFLICTING_RULE"
    assert by_rule[rule_by_logical(client, CATCH_ALL_DUTIES)["rule_id"]]["code"] == "OVERRIDES_RULE"
    assert by_rule[rule_by_logical(client, R102)["rule_id"]]["code"] == "OVERRIDDEN_BY_RULE"


def test_admin_deletes_every_version_of_a_published_rule(client, db_dsn):
    rev1 = rule_by_logical(client, SHIPPING_TWO_REVS, revision=1)
    rev2 = rule_by_logical(client, SHIPPING_TWO_REVS, revision=2)
    assert delete(client, rev2, reason="").status_code == 422

    resp = delete(client, rev2)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["deleted_rule_ids"]) == {rev1["rule_id"], rev2["rule_id"]}

    assert delete(client, rev2).status_code == 409
    listed = {r["logical_rule_id"] for r in client.get("/billing/rules", params={"include_ended": True},
                                                       headers=VIEWER).json()}
    assert SHIPPING_TWO_REVS not in listed
    deleted = client.get("/billing/rules", params={"status": "DELETED", "include_ended": True}, headers=VIEWER).json()
    assert {r["rule_id"] for r in deleted} == {rev1["rule_id"], rev2["rule_id"]}
    assert client.post(f"/billing/rules/{rev2['rule_id']}/clone", json={}, headers=EDITOR).status_code == 409

    history = client.get(f"/billing/rules/{rev2['rule_id']}/history", headers=VIEWER).json()
    assert [h["action"] for h in history[:2]] == ["DELETED", "DELETED"]
    assert history[0]["reason"] == "created in error"
    with psycopg.connect(db_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM billing_config.v_sync_billing_rule WHERE logical_rule_id = %s",
                            (SHIPPING_TWO_REVS,)).fetchone()[0] == 0


def test_deleting_a_draft_revision_keeps_the_published_rule(client):
    published = rule_by_logical(client, CATCH_ALL_DUTIES)
    draft = client.post(f"/billing/rules/{published['rule_id']}/clone", json={}, headers=EDITOR).json()
    resp = delete(client, draft)
    assert resp.json()["deleted_rule_ids"] == [draft["rule_id"]]
    assert client.get(f"/billing/rules/{published['rule_id']}", headers=VIEWER).json()["status"] == "PUBLISHED"


def test_deleted_rule_no_longer_conflicts(client):
    rule = create(client, merchant_id="M123", effective_from=future(60), conditions=[dest("CA", "US", "MX"), carrier("UPS", "DHL_EXPRESS")])
    assert publish(client, rule).status_code == 422
    assert delete(client, rule_by_logical(client, R101)).status_code == 200
    rule = client.get(f"/billing/rules/{rule['rule_id']}", headers=VIEWER).json()
    assert publish(client, rule).status_code == 200


def test_successor_revision_end_dates_predecessor(client):
    base = rule_by_logical(client, CATCH_ALL_DUTIES)
    draft = client.post(f"/billing/rules/{base['rule_id']}/clone", json={}, headers=EDITOR).json()
    assert draft["revision"] == 2 and draft["logical_rule_id"] == CATCH_ALL_DUTIES
    assert client.post(f"/billing/rules/{base['rule_id']}/clone", json={}, headers=EDITOR).status_code == 409

    start = future(10)
    draft = client.patch(f"/billing/rules/{draft['rule_id']}",
                         json={"row_version": draft["row_version"], "effective_from": start, "is_waived": True},
                         headers=EDITOR).json()
    resp = publish(client, draft)
    assert resp.status_code == 200, resp.text
    assert "WILL_END_DATE" in codes(resp.json()["validation"])

    old = client.get(f"/billing/rules/{base['rule_id']}", headers=VIEWER).json()
    assert old["status"] == "PUBLISHED"
    assert datetime.fromisoformat(old["effective_to"]) == datetime.fromisoformat(start)
    assert [r["revision"] for r in old["revisions"]] == [2, 1]


def test_successor_starting_earlier_voids_future_predecessor(client, db_dsn):
    rev1 = create(client, effective_from=future(20))
    assert publish(client, rev1).status_code == 200
    rev2 = client.post(f"/billing/rules/{rev1['rule_id']}/clone", json={}, headers=EDITOR).json()
    rev2 = client.patch(f"/billing/rules/{rev2['rule_id']}",
                        json={"row_version": rev2["row_version"], "effective_from": future(10)}, headers=EDITOR).json()
    assert publish(client, rev2).status_code == 200

    old = client.get(f"/billing/rules/{rev1['rule_id']}", headers=VIEWER).json()
    assert old["status"] == "RETIRED" and old["effective_to"] == old["effective_from"]
    with psycopg.connect(db_dsn) as conn:
        synced = {r[0] for r in conn.execute(
            "SELECT rule_id::text FROM billing_config.v_sync_billing_rule WHERE logical_rule_id = %s",
            (rev1["logical_rule_id"],))}
    assert synced == {rev2["rule_id"]}


def test_stale_row_version_is_rejected(client):
    rule = create(client)
    ok = client.patch(f"/billing/rules/{rule['rule_id']}", json={"row_version": rule["row_version"], "is_waived": True},
                      headers=EDITOR)
    assert ok.status_code == 200
    stale = client.patch(f"/billing/rules/{rule['rule_id']}", json={"row_version": rule["row_version"], "is_waived": False},
                         headers=EDITOR)
    assert stale.status_code == 409
    assert publish(client, rule).status_code == 409


@pytest.mark.parametrize("conditions, code", [
    ([{"dimension": "CARRIER", "values": []}], "EMPTY_DIMENSION"),
    ([{"dimension": "CARRIER", "values": ["UPS", "ups"]}], "DUPLICATE_VALUES"),
    ([{"dimension": "CARRIER", "values": ["UPS"]}, {"dimension": "carrier", "values": ["FEDEX"]}], "DUPLICATE_DIMENSION"),
])
def test_malformed_conditions_rejected_on_save(client, conditions, code):
    resp = client.post("/billing/rules", json={"merchant_id": "M123", "fee_type": "duties", "conditions": conditions},
                       headers=EDITOR)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == code


def test_validation_errors(client):
    rule = create(client, payer_type="PARTNER", effective_from=future(5), effective_to=future(1), conditions=[
        {"dimension": "SERVICE_LEVEL", "values": ["EXPRESS"]},
        {"dimension": "CARRIER", "values": ["NOT_A_CARRIER"]},
    ])
    v = client.post(f"/billing/rules/{rule['rule_id']}/validate", headers=EDITOR).json()
    assert not v["ok"]
    assert {"PARTNER_REFERENCE_REQUIRED", "DIMENSION_NOT_STAGED", "INVALID_DIMENSION_VALUE",
            "END_BEFORE_START"} <= codes(v["issues"], "error")


def test_retroactive_start_requires_admin(client):
    rule = create(client, effective_from="2026-01-01T00:00:00Z")
    assert "RETROACTIVE" in codes(client.post(f"/billing/rules/{rule['rule_id']}/validate", headers=EDITOR).json()["issues"], "error")
    v = client.post(f"/billing/rules/{rule['rule_id']}/validate", headers=ADMIN).json()
    assert v["ok"] and "RETROACTIVE" in codes(v["issues"], "warning")


def test_retire_keeps_historical_window(client, db_dsn):
    rule = create(client, effective_from=future(2))
    published = publish(client, rule).json()
    resp = client.post(f"/billing/rules/{rule['rule_id']}/retire",
                       json={"row_version": published["row_version"], "effective_at": future(40), "reason": "ended"},
                       headers=PUBLISHER)
    assert resp.status_code == 200, resp.text
    retired = resp.json()
    assert retired["status"] == "RETIRED"
    assert datetime.fromisoformat(retired["effective_to"]) == datetime.fromisoformat(future(40))
    with psycopg.connect(db_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM billing_config.v_sync_billing_rule WHERE rule_id = %s",
                            (rule["rule_id"],)).fetchone()[0] == 1


def test_applicable_filter_includes_all_value_rules(client):
    rows = client.get("/billing/rules", params={"merchant_id": "M123", "fee_type": "duties",
                                                "dimension": "CARRIER", "value": "UPS"}, headers=VIEWER).json()
    logical = {r["logical_rule_id"] for r in rows}
    assert CATCH_ALL_DUTIES in logical      # no carrier condition => ALL
    assert R101 not in logical              # DHL/FedEx only


def test_published_rule_cannot_be_edited(client):
    r101 = rule_by_logical(client, R101)
    resp = client.patch(f"/billing/rules/{r101['rule_id']}", json={"row_version": r101["row_version"], "is_waived": False},
                        headers=ADMIN)
    assert resp.status_code == 409


def test_seed_draft_fails_validation(client):
    draft = rule_by_logical(client, M789_DRAFT)
    v = client.post(f"/billing/rules/{draft['rule_id']}/validate", headers=EDITOR).json()
    assert "PARTNER_REFERENCE_REQUIRED" in codes(v["issues"], "error")
