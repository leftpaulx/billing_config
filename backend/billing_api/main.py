"""Billing configuration API (spec §9.1). Serves the rule editor UI at /."""

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import rules
from .auth import User, current_user, require
from .db import make_pool
from .errors import ApiError, not_found
from .schemas import CloneRequest, DeleteRequest, PublishRequest, RetireRequest, RuleCreate, RuleUpdate

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


def create_app(dsn: str | None = None) -> FastAPI:
    dsn = dsn or os.environ["DATABASE_URL"]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = make_pool(dsn)
        yield
        app.state.pool.close()

    app = FastAPI(title="OpenBorder Billing Configuration API (POC)", lifespan=lifespan)

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content=exc.to_body())

    def conn(request: Request):
        with request.app.state.pool.connection() as c:
            yield c

    # ------------------------------------------------------------------ identity

    @app.get("/billing/me")
    def me(user: User = Depends(current_user)):
        return {"user": user.id, "role": user.role}

    # ------------------------------------------------------------------ reference data

    @app.get("/billing/dimensions")
    def list_dimensions(c=Depends(conn), _: User = Depends(current_user)):
        return c.execute("SELECT * FROM billing_dimension_catalog ORDER BY sort_order, dimension_key").fetchall()

    @app.get("/billing/dimensions/{key}/values")
    def dimension_values(key: str, q: str | None = None, limit: int = Query(500, le=2000),
                         c=Depends(conn), _: User = Depends(current_user)):
        if not c.execute("SELECT 1 FROM billing_dimension_catalog WHERE dimension_key = %s", (key.upper(),)).fetchone():
            raise not_found("Dimension")
        return c.execute(
            """SELECT dimension_value AS value, display_name, is_active FROM ref_dimension_value
               WHERE dimension_key = %(k)s
                 AND (%(q)s::text IS NULL OR dimension_value ILIKE %(like)s OR display_name ILIKE %(like)s)
               ORDER BY display_name LIMIT %(limit)s""",
            {"k": key.upper(), "q": q, "like": f"%{q}%", "limit": limit},
        ).fetchall()

    @app.get("/billing/reference/merchants")
    def merchants(c=Depends(conn), _: User = Depends(current_user)):
        return c.execute("SELECT * FROM ref_merchant ORDER BY merchant_id").fetchall()

    @app.get("/billing/reference/fee-types")
    def fee_types(c=Depends(conn), _: User = Depends(current_user)):
        return c.execute("SELECT * FROM ref_fee_type ORDER BY display_name").fetchall()

    @app.get("/billing/reference/partners")
    def partners(c=Depends(conn), _: User = Depends(current_user)):
        return c.execute("SELECT * FROM ref_partner ORDER BY partner_id").fetchall()

    # ------------------------------------------------------------------ rules

    @app.get("/billing/rules")
    def list_rules(
        q: str | None = None,
        merchant_id: str | None = None,
        fee_type: str | None = None,
        status: list[str] | None = Query(None),
        effective_on: datetime | None = None,
        dimension: str | None = None,
        value: str | None = None,
        include_ended: bool = False,
        c=Depends(conn),
        _: User = Depends(current_user),
    ):
        return rules.list_rules(c, q=q, merchant_id=merchant_id, fee_type=fee_type, statuses=status,
                                effective_on=effective_on, dimension=dimension, value=value,
                                include_ended=include_ended)

    @app.get("/billing/rules/{rule_id}")
    def get_rule(rule_id: str, c=Depends(conn), _: User = Depends(current_user)):
        return rules.get_rule_detail(c, _uuid(rule_id))

    @app.post("/billing/rules", status_code=201)
    def create_rule(body: RuleCreate, c=Depends(conn), user: User = Depends(require("EDITOR"))):
        return rules.create_draft(c, body, user)

    @app.patch("/billing/rules/{rule_id}")
    def update_rule(rule_id: str, body: RuleUpdate, c=Depends(conn), user: User = Depends(require("EDITOR"))):
        return rules.update_draft(c, _uuid(rule_id), body, user)

    @app.delete("/billing/rules/{rule_id}")
    def delete_rule(rule_id: str, body: DeleteRequest, c=Depends(conn), user: User = Depends(require("ADMIN"))):
        return {"deleted_rule_ids": rules.delete_rule(c, _uuid(rule_id), body.row_version, body.reason, user)}

    @app.post("/billing/rules/{rule_id}/validate")
    def validate_rule(rule_id: str, c=Depends(conn), user: User = Depends(require("EDITOR"))):
        result = rules.validate(c, _uuid(rule_id), user)
        return {"ok": result.ok, "issues": result.issues}

    @app.post("/billing/rules/{rule_id}/publish")
    def publish_rule(rule_id: str, body: PublishRequest, c=Depends(conn), user: User = Depends(require("PUBLISHER"))):
        return rules.publish_rule(c, _uuid(rule_id), body.row_version, body.reason, user)

    @app.post("/billing/rules/{rule_id}/retire")
    def retire_rule(rule_id: str, body: RetireRequest, c=Depends(conn), user: User = Depends(require("PUBLISHER"))):
        return rules.retire_rule(c, _uuid(rule_id), body.row_version, body.effective_at, body.reason, user)

    @app.post("/billing/rules/{rule_id}/clone", status_code=201)
    def clone_rule(rule_id: str, body: CloneRequest | None = None, c=Depends(conn),
                   user: User = Depends(require("EDITOR"))):
        return rules.clone_rule(c, _uuid(rule_id), (body or CloneRequest()).as_new_rule, user)

    @app.get("/billing/rules/{rule_id}/history")
    def history(rule_id: str, c=Depends(conn), _: User = Depends(current_user)):
        return rules.get_history(c, _uuid(rule_id))

    # ------------------------------------------------------------------ UI
    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")

    return app


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise not_found("Rule") from None

