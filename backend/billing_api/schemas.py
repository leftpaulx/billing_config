"""Request payloads (spec §9.2)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

PayerType = Literal["MERCHANT", "CARRIER", "PARTNER"]


class ConditionIn(BaseModel):
    dimension: str
    # v1 supports only IN; kept in the contract to make semantics explicit.
    operator: Literal["IN"] = "IN"
    values: list[str]


class RuleCreate(BaseModel):
    merchant_id: str
    fee_type: str
    payer_type: PayerType | None = None
    payer_reference: str | None = None
    is_waived: bool = False
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    conditions: list[ConditionIn] = Field(default_factory=list)
    change_reason: str | None = None


class RuleUpdate(BaseModel):
    """Partial update of a draft. Only fields present in the body are changed;
    `conditions`, when present, replaces the full condition set."""

    row_version: int
    merchant_id: str | None = None
    fee_type: str | None = None
    payer_type: PayerType | None = None
    payer_reference: str | None = None
    is_waived: bool | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    conditions: list[ConditionIn] | None = None
    change_reason: str | None = None


class PublishRequest(BaseModel):
    row_version: int
    reason: str | None = None


class DeleteRequest(BaseModel):
    row_version: int
    reason: str


class RetireRequest(BaseModel):
    row_version: int
    effective_at: datetime | None = None  # defaults to now
    reason: str


class CloneRequest(BaseModel):
    # False: successor revision of the same logical rule ("Duplicate as New Revision").
    # True: an independent new rule seeded from this one ("Duplicate").
    as_new_rule: bool = False
