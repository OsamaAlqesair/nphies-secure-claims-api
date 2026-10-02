"""Immutable internal validation facts; no responses, ORM entities or transactions."""

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from services.claim_business import BusinessResult
from services.coverage import CoverageDecision
from services.diagnosis_catalog import MISSING_CATALOG_CODE


class ValidationResult(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class TerminologyStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class InsurerContext:
    reference: str
    fhir_id: str
    identifiers: tuple[tuple[str, str], ...]
    insurance_company_id: int | None = None


@dataclass(frozen=True)
class ValidationFinding:
    reason: str
    diagnostics: str
    expression: tuple[str, ...] | None = None
    code: Literal["business-rule", "not-found"] = "business-rule"


@dataclass(frozen=True)
class ValidationObservation:
    """One occurrence, retaining the evaluator's frozen result without more reads.

    A missing coverage decision means coverage was not evaluated, whereas a
    decision with scope NONE records an evaluated mapping/rule failure.
    """

    item_sequence: int
    diagnosis_sequence: int
    diagnosis_system: str
    insurer: InsurerContext
    business_result: BusinessResult

    @property
    def diagnosis_code(self) -> str:
        return self.business_result.pair.diagnosis_code

    @property
    def service_code(self) -> str:
        return self.business_result.pair.service_code

    @property
    def service_system(self) -> str | None:
        return self.business_result.pair.service_system

    @property
    def terminology_status(self) -> TerminologyStatus:
        if self.business_result.reason == MISSING_CATALOG_CODE:
            return TerminologyStatus.UNAVAILABLE
        if self.business_result.reason in {
            "unknown_diagnosis",
            "unknown_service",
            "ambiguous_terminology",
        }:
            return TerminologyStatus.INVALID
        return TerminologyStatus.VALID

    @property
    def coverage(self) -> CoverageDecision | None:
        return self.business_result.coverage

    @property
    def reason(self) -> str | None:
        return self.business_result.reason


@dataclass(frozen=True)
class ValidationTermination:
    stage: Literal["principal_diagnosis", "insurer_resolution", "terminology"]
    reason: str
    item_sequence: int | None = None
    diagnosis_sequence: int | None = None


@dataclass(frozen=True)
class ClaimValidationReport:
    """Business validation only: model rejection happens before this report.

    Findings retain all observed failures, even when catalog unavailability
    takes precedence in the existing issue-only adapter. A termination marks
    an interrupted traversal; skipped items have invalid service coding.
    """

    result: ValidationResult
    insurer: InsurerContext
    observations: tuple[ValidationObservation, ...]
    findings: tuple[ValidationFinding, ...]
    skipped_item_sequences: tuple[int, ...] = ()
    termination: ValidationTermination | None = None
