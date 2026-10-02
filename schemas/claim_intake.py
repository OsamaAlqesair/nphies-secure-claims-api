"""Public creation result; historical snapshots and internal IDs stay private."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict

from schemas.claim import OperationOutcome


class ClaimIntakeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    public_id: UUID
    result: Literal["PASSED", "FAILED", "UNAVAILABLE"]
    reason: Literal["validation_passed", "validation_failed", "validation_unavailable"]
    operation_outcome: OperationOutcome
    created_at: AwareDatetime
    attempt_no: Literal[1] = 1
