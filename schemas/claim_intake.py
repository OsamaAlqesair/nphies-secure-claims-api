"""Explicit claim intake creation and immutable historical read contracts."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_serializer,
    field_validator,
    model_validator,
)

from schemas.claim import OperationOutcome
from schemas.fhir_claim import ClaimSubmission


class ClaimIntakeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    public_id: UUID
    result: Literal["PASSED", "FAILED", "UNAVAILABLE"]
    reason: Literal["validation_passed", "validation_failed", "validation_unavailable"]
    operation_outcome: OperationOutcome
    created_at: AwareDatetime
    attempt_no: Literal[1] = 1


def canonical_public_id(value):
    try:
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError()
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise ValueError("A canonical public UUID is required.") from None


CanonicalPublicID = Annotated[UUID, BeforeValidator(canonical_public_id)]
ValidationResultCode = Literal["PASSED", "FAILED", "UNAVAILABLE"]
ValidationReason = Literal[
    "validation_passed", "validation_failed", "validation_unavailable"
]
PositiveNumber = Annotated[StrictInt, Field(gt=0)]


class HistoricalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimIntakeSummary(HistoricalModel):
    public_id: UUID
    created_at: AwareDatetime
    state: Literal["validated", "validation_failed", "validation_unavailable"]
    latest_attempt_no: PositiveNumber
    latest_result: ValidationResultCode
    latest_reason: ValidationReason


class ClaimIntakeList(HistoricalModel):
    items: list[ClaimIntakeSummary]
    limit: Annotated[StrictInt, Field(ge=1, le=100)]
    offset: Annotated[StrictInt, Field(ge=0)]
    total: Annotated[StrictInt, Field(ge=0)]


class ClaimIntakeDetail(ClaimIntakeSummary):
    submission: ClaimSubmission


class HistoricalInsurer(HistoricalModel):
    reference: str
    fhir_id: str
    identifiers: list[tuple[str, str]]
    insurance_company_id: PositiveNumber | None


class HistoricalTerm(HistoricalModel):
    code: str
    system: str
    display: str | None


class HistoricalPair(HistoricalModel):
    diagnosis_code: str
    service_code: str
    service_system: str | None
    insurer_id: PositiveNumber | None


class HistoricalCoverage(HistoricalModel):
    status: Literal[
        "approved",
        "denied",
        "diagnosis_mapping_missing",
        "service_mapping_missing",
        "no_applicable_rule",
    ]
    diagnosis_id: PositiveNumber | None
    service_id: PositiveNumber | None
    matched_rule_ids: list[PositiveNumber]
    selected_scope: Literal["insurer-specific", "global", "none"]
    insurer_id: PositiveNumber | None
    is_covered: StrictBool


class HistoricalBusinessResult(HistoricalModel):
    pair: HistoricalPair
    diagnosis: HistoricalTerm | None
    service: HistoricalTerm | None
    reason: str | None
    is_valid: StrictBool
    coverage: HistoricalCoverage | None


class HistoricalObservation(HistoricalModel):
    item_sequence: PositiveNumber
    diagnosis_sequence: PositiveNumber
    diagnosis_system: str
    diagnosis_code: str
    service_code: str
    service_system: str | None
    insurer: HistoricalInsurer
    terminology_status: Literal["valid", "invalid", "unavailable"]
    reason: str | None
    business_result: HistoricalBusinessResult


class HistoricalFinding(HistoricalModel):
    reason: str
    diagnostics: str
    expression: list[str] | None
    code: Literal["business-rule", "not-found"]


class HistoricalTermination(HistoricalModel):
    stage: Literal["principal_diagnosis", "insurer_resolution", "terminology"]
    reason: str
    item_sequence: PositiveNumber | None
    diagnosis_sequence: PositiveNumber | None


class HistoricalValidationReport(HistoricalModel):
    schema_version: Literal["claim-validation-report-v1"]
    result: Literal["passed", "failed", "unavailable"]
    insurer: HistoricalInsurer
    observations: list[HistoricalObservation]
    findings: list[HistoricalFinding]
    skipped_item_sequences: list[PositiveNumber]
    termination: HistoricalTermination | None


class ClaimValidationHistoryItem(HistoricalModel):
    attempt_no: PositiveNumber
    result: ValidationResultCode
    reason: ValidationReason
    occurred_at: AwareDatetime
    operation_outcome: OperationOutcome
    validation_report: HistoricalValidationReport

    @model_validator(mode="after")
    def consistent_result(self):
        if (
            self.reason != "validation_" + self.result.lower()
            or self.validation_report.result != self.result.lower()
        ):
            raise ValueError("Historical validation result is inconsistent.")
        return self

    @field_serializer("operation_outcome", when_used="json")
    def outcome_snapshot(self, value):
        # Keep explicit historical nulls, without adding absent optional fields.
        return value.model_dump(mode="json", exclude_unset=True)


class TimelineDetails(HistoricalModel):
    attempt_no: PositiveNumber | None = None
    result: ValidationResultCode | None = None

    @field_validator("attempt_no", "result")
    @classmethod
    def non_null_metadata(cls, value):
        if value is None:
            raise ValueError("Historical event metadata must not be null.")
        return value


class ClaimIntakeTimelineItem(HistoricalModel):
    event_no: PositiveNumber
    event_type: Literal["intake.created", "validation.completed"]
    occurred_at: AwareDatetime
    reason: Literal["intake_accepted", "validation_completed"]
    details: TimelineDetails

    @model_validator(mode="after")
    def consistent_event(self):
        expected = (
            "intake_accepted"
            if self.event_type == "intake.created"
            else "validation_completed"
        )
        if self.reason != expected:
            raise ValueError("Historical event reason is inconsistent.")
        return self

    @field_serializer("details", when_used="json")
    def controlled_details(self, value):
        return value.model_dump(mode="json", exclude_unset=True)
