"""FHIR extraction adapter for the shared terminology and coverage services."""

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from models import Organization, InsuranceCompany
from schemas.fhir_claim import ClaimSubmission
from schemas.claim import Issue
from services.claim_business import BusinessPair, ClaimBusinessEvaluator
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


def evaluate(db: Session, submission: ClaimSubmission) -> list[Issue]:
    claim = submission.claim
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
        return [
            rejection(
                "invalid_diagnosis",
                "Exactly one principal diagnosis is required.",
                ["Claim.diagnosis"],
            )
        ]
    insurer = submission.insurer
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
        return [
            rejection(
                "unknown_insurer",
                "Insurer reference and license are not linked to an active insurance company.",
                ["Claim.insurer"],
            )
        ]

    diagnoses = {diagnosis.sequence: diagnosis for diagnosis in claim.diagnosis}
    issues = []
    # Organization lookup above has already resolved an available insurer.
    evaluator = ClaimBusinessEvaluator(db, verify_insurer=False)
    for item in claim.item:
        expression = [f"Claim.item.where(sequence = {item.sequence}).productOrService"]
        if len(item.productOrService.coding) != 1:
            issues.append(
                rejection(
                    "invalid_service",
                    "Exactly one service coding is required.",
                    expression,
                )
            )
            continue
        service = item.productOrService.coding[0]
        # Always validate the principal, plus every additional linked diagnosis.
        sequences = dict.fromkeys([principal[0].sequence, *item.diagnosisSequence])
        for sequence in sequences:
            diagnosis = diagnoses[sequence].diagnosisCodeableConcept.coding[0]
            location = expression + [
                f"Claim.diagnosis.where(sequence = {sequence}).diagnosisCodeableConcept"
            ]
            result = evaluator.evaluate(
                BusinessPair(diagnosis.code, service.code, insurer_id, service.system)
            )
            if result.reason == MISSING_CATALOG_CODE:
                issue = rejection(
                    MISSING_CATALOG_CODE, MISSING_CATALOG_MESSAGE, ["Claim.diagnosis"]
                )
                issue.code = "not-found"
                return [issue]
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
                issues.append(rejection(result.reason, message, location))
                continue
            decision = result.coverage
            if not decision.is_covered:
                reason, message = COVERAGE_FAILURES[decision.status]
                if decision.status is CoverageStatus.DENIED:
                    message = (
                        f"Medical Necessity Denied: item {item.sequence}, "
                        f"service {service.code}, diagnosis {diagnosis.code}."
                    )
                issues.append(rejection(reason, message, location))
    return issues


async def validate_coverage(
    db: AsyncSession, submission: ClaimSubmission
) -> list[Issue]:
    # SQLAlchemy's greenlet bridge awaits driver I/O inside the existing sync
    # services. It does not run blocking synchronous database I/O on this loop.
    return await db.run_sync(evaluate, submission)
