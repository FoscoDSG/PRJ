"""Strict request bodies. Extra fields are rejected, including recommendation and confidence."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SourceClass = Literal["system_of_record", "operational", "external", "manual", "unknown"]
_HASH = r"^[0-9a-f]{64}$"


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SqlIngestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str = Field(min_length=1, max_length=128)
    source_name: str | None = Field(default=None, max_length=128)
    source_class: SourceClass


class ApproveBody(BaseModel):
    """The client sends the decision and the evidence hash it previewed.

    Recommendation text and confidence are not accepted: the server reads both
    from the stored recommendation.
    """

    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve"]
    evidence_hash: str = Field(pattern=_HASH)


class ModifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["modify"]
    evidence_hash: str = Field(pattern=_HASH)
    quantity: int = Field(gt=0, le=1_000_000)


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["reject"]
    evidence_hash: str = Field(pattern=_HASH)
    reason: str = Field(min_length=5, max_length=500)


class StockBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on_hand: int = Field(ge=0, le=10_000_000)
