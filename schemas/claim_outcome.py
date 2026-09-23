"""FHIR R4 OperationOutcome for pre-validation, including successful checks."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class OutcomeIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Literal["error", "information"]
    code: Literal[
        "business-rule",
        "not-found",
        "invalid",
        "informational",
        "transient",
        "required",
        "structure",
        "invariant",
        "login",
        "forbidden",
    ]
    diagnostics: str
    expression: list[str] = Field(
        default_factory=list,
        description="FHIRPath locations within the Claim resource.",
    )


class ClaimOperationOutcome(BaseModel):
    resourceType: Literal["OperationOutcome"] = "OperationOutcome"
    issue: list[OutcomeIssue] = Field(min_length=1)
