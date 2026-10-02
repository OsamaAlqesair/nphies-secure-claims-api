"""FHIR extraction and issue adapters for the shared business evaluator."""

from dataclasses import replace

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from models import Organization, InsuranceCompany
from schemas.fhir_claim import ClaimSubmission
from schemas.claim import Issue
from services.claim_business import BusinessPair, ClaimBusinessEvaluator
from services.claim_validation_report import (
    ClaimValidationReport,
    InsurerContext,
    ValidationFinding,
    ValidationObservation,
    ValidationResult,
    ValidationTermination,
)
from services.coverage import COVERAGE_FAILURES, CoverageStatus
from services.diagnosis_catalog import (
    MISSING_CATALOG_CODE,
    MISSING_CATALOG_MESSAGE,
)


def rejection(
    reason: str, diagnostics: str, expression: list[str] | None = None
) -> Issue:
    return Issue(
        code="business-rule",
        diagnostics=diagnostics,
        expression=expression,
        details={
            "coding": [
                {"system": "urn:nphies-secure-claim-api:validation", "code": reason}
            ]
        },
    )


def evaluate_report(db: Session, submission: ClaimSubmission) -> ClaimValidationReport:
    # Include the adapter's insurer lookup in the evaluator's read-only boundary.
    with db.no_autoflush:
        return _evaluate_report(db, submission)


def _evaluate_report(db: Session, submission: ClaimSubmission) -> ClaimValidationReport:
    claim = submission.claim
    insurer = submission.insurer
    insurer_context = InsurerContext(
        reference=claim.insurer.reference,
        fhir_id=insurer.id,
        identifiers=tuple((entry.system, entry.value) for entry in insurer.identifier),
    )
    findings = []
    observations = []
    skipped_items = []

    def finish(termination=None):
        unavailable = (
            termination is not None and termination.reason == MISSING_CATALOG_CODE
        )
        return ClaimValidationReport(
            result=(
                ValidationResult.UNAVAILABLE
                if unavailable
                else ValidationResult.FAILED if findings else ValidationResult.PASSED
            ),
            insurer=insurer_context,
            observations=tuple(observations),
            findings=tuple(findings),
            skipped_item_sequences=tuple(skipped_items),
            termination=termination,
        )

    principal = [
        diagnosis
        for diagnosis in claim.diagnosis
        if any(
            coding.code == "principal"
            and coding.system
            == "http://nphies.sa/terminology/CodeSystem/diagnosis-type"
            for concept in diagnosis.type
            for coding in concept.coding
        )
    ]
    if len(principal) != 1:
        findings.append(
            ValidationFinding(
                "invalid_diagnosis",
                "Exactly one principal diagnosis is required.",
                ("Claim.diagnosis",),
            )
        )
        return finish(ValidationTermination("principal_diagnosis", "invalid_diagnosis"))
    insurer_id = db.scalar(
        select(InsuranceCompany.id)
        .join(Organization, InsuranceCompany.organization_id == Organization.id)
        .where(
            Organization.fhir_id == insurer.id,
            tuple_(Organization.identifier_system, Organization.identifier_value).in_(
                [
                    (identifier.system, identifier.value)
                    for identifier in insurer.identifier
                ]
            ),
        )
    )
    if insurer_id is None:
        findings.append(
            ValidationFinding(
                "unknown_insurer",
                "Insurer reference and license are not linked to an active insurance company.",
                ("Claim.insurer",),
            )
        )
        return finish(ValidationTermination("insurer_resolution", "unknown_insurer"))

    insurer_context = replace(insurer_context, insurance_company_id=insurer_id)

    diagnoses = {diagnosis.sequence: diagnosis for diagnosis in claim.diagnosis}
    # Organization lookup above has already resolved an available insurer.
    evaluator = ClaimBusinessEvaluator(db, verify_insurer=False)
    for item in claim.item:
        expression = (f"Claim.item.where(sequence = {item.sequence}).productOrService",)
        if len(item.productOrService.coding) != 1:
            findings.append(
                ValidationFinding(
                    "invalid_service",
                    "Exactly one service coding is required.",
                    expression,
                )
            )
            skipped_items.append(item.sequence)
            continue
        service = item.productOrService.coding[0]
        # Always validate the principal, plus every additional linked diagnosis.
        sequences = dict.fromkeys([principal[0].sequence, *item.diagnosisSequence])
        for sequence in sequences:
            diagnosis = diagnoses[sequence].diagnosisCodeableConcept.coding[0]
            location = expression + (
                f"Claim.diagnosis.where(sequence = {sequence}).diagnosisCodeableConcept",
            )
            result = evaluator.evaluate(
                BusinessPair(diagnosis.code, service.code, insurer_id, service.system)
            )
            observations.append(
                ValidationObservation(
                    item.sequence, sequence, diagnosis.system, insurer_context, result
                )
            )
            if result.reason == MISSING_CATALOG_CODE:
                findings.append(
                    ValidationFinding(
                        MISSING_CATALOG_CODE,
                        MISSING_CATALOG_MESSAGE,
                        ("Claim.diagnosis",),
                        code="not-found",
                    )
                )
                return finish(
                    ValidationTermination(
                        "terminology", MISSING_CATALOG_CODE, item.sequence, sequence
                    )
                )
            if result.reason in {
                "unknown_diagnosis",
                "unknown_service",
                "ambiguous_terminology",
            }:
                message = {
                    "unknown_diagnosis": "Diagnosis code is not active",
                    "unknown_service": "Service code is not active",
                    "ambiguous_terminology": "Code matches multiple active terminology entries.",
                }[result.reason]
                findings.append(ValidationFinding(result.reason, message, location))
                continue
            decision = result.coverage
            if not decision.is_covered:
                reason, message = COVERAGE_FAILURES[decision.status]
                if decision.status is CoverageStatus.DENIED:
                    message = (
                        f"Medical Necessity Denied: item {item.sequence}, "
                        f"service {service.code}, diagnosis {diagnosis.code}."
                    )
                findings.append(ValidationFinding(reason, message, location))
    return finish()


def report_issues(report: ClaimValidationReport) -> list[Issue]:
    """Preserve catalog failure precedence without discarding observed report facts."""
    findings = report.findings
    if report.result is ValidationResult.UNAVAILABLE:
        findings = tuple(
            entry for entry in findings if entry.reason == MISSING_CATALOG_CODE
        )
    issues = []
    for finding in findings:
        issue = rejection(
            finding.reason,
            finding.diagnostics,
            list(finding.expression) if finding.expression is not None else None,
        )
        issue.code = finding.code
        issues.append(issue)
    return issues


def evaluate(db: Session, submission: ClaimSubmission) -> list[Issue]:
    return report_issues(evaluate_report(db, submission))


async def validate_report(
    db: AsyncSession, submission: ClaimSubmission
) -> ClaimValidationReport:
    return await db.run_sync(evaluate_report, submission)


async def validate_coverage(
    db: AsyncSession, submission: ClaimSubmission
) -> list[Issue]:
    # SQLAlchemy's greenlet bridge awaits driver I/O inside the existing sync
    # services. It does not run blocking synchronous database I/O on this loop.
    return await db.run_sync(evaluate, submission)
