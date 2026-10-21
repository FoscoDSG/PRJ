"""Strict request bodies. Extra fields are rejected, including recommendation and confidence."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SourceClass = Literal["system_of_record", "operational", "external", "manual", "unknown"]
_HASH = r"^[0-9a-f]{64}$"


class EmptyBody(BaseModel):
    """No client fields. company_id is not accepted."""

    model_config = ConfigDict(extra="forbid")


class FileTypeDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["correct", "wrong", "not_sure"]
    correct_type: str | None = None


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


class RevokeBody(BaseModel):
    """Motive of a revocation. company_id is not accepted."""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)


class UnmaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: UUID
    reason: str = Field(min_length=5, max_length=500)


class StockBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on_hand: int = Field(ge=0, le=10_000_000)


class PaymentConfirmBody(BaseModel):
    """A person confirms a proposed match. The rule does not close the invoice."""

    model_config = ConfigDict(extra="forbid")
    decision: Literal["confirm"]
    evidence_hash: str = Field(pattern=_HASH)


class PaymentCorrectBody(BaseModel):
    """``invoice_number`` is the invoice number inside the caller's company.

    ``invoice_id`` remains for a caller that already has the id. One of the
    two is required. The server resolves the number; the page does not.
    """

    model_config = ConfigDict(extra="forbid")
    decision: Literal["correct"]
    evidence_hash: str = Field(pattern=_HASH)
    invoice_id: UUID | None = None
    invoice_number: str | None = Field(default=None, min_length=1, max_length=128)
    allocated_amount: float = Field(gt=0, le=10_000_000)
    reason: str = Field(min_length=5, max_length=500)


class PaymentRejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["reject"]
    evidence_hash: str = Field(pattern=_HASH)
    reason: str = Field(min_length=5, max_length=500)


class UnmatchedPaymentBody(BaseModel):
    """A person allocates a payment the rule left open.

    The invoice is the number inside the caller's company, or an id the
    caller already holds. The server does not look in another company.
    """

    model_config = ConfigDict(extra="forbid")
    invoice_id: UUID | None = None
    invoice_number: str | None = Field(default=None, min_length=1, max_length=128)
    allocated_amount: float = Field(gt=0, le=10_000_000)
    reason: str = Field(min_length=5, max_length=500)
