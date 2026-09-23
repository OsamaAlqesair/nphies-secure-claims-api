"""FHIR extraction adapter for the shared terminology and coverage services."""

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from models import Organization, InsuranceCompany
from schemas.fhir_claim import ClaimSubmission
from schemas.claim import Issue
from services.terminology import (
    find_term,
    DIAGNOSIS_SYSTEM,
    SERVICE_SYSTEMS,
    AmbiguousTerminologyError,
)
from services.coverage import resolve_coverage_by_codes


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
    # Memoization shares decisions for repeated pairs without bypassing per-item checks.
    terms = {}
    decisions = {}
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
            try:
                diagnosis_key = (diagnosis.code, (DIAGNOSIS_SYSTEM,))
                service_key = (service.code, SERVICE_SYSTEMS)
                if diagnosis_key not in terms:
                    terms[diagnosis_key] = find_term(
                        db, diagnosis.code, (DIAGNOSIS_SYSTEM,)
                    )
                if service_key not in terms:
                    terms[service_key] = find_term(db, service.code, SERVICE_SYSTEMS)
                if terms[diagnosis_key] is None:
                    issues.append(
                        rejection(
                            "unknown_diagnosis",
                            "Diagnosis code is not active",
                            location,
                        )
                    )
                    continue
                if (
                    terms[service_key] is None
                    or terms[service_key].code_system_url != service.system
                ):
                    issues.append(
                        rejection(
                            "unknown_service", "Service code is not active", location
                        )
                    )
                    continue
            except AmbiguousTerminologyError:
                issues.append(
                    rejection(
                        "ambiguous_terminology",
                        "Code matches multiple active terminology entries.",
                        location,
                    )
                )
                continue
            key = (diagnosis.code, service.code)
            if key not in decisions:
                decisions[key] = resolve_coverage_by_codes(
                    db, diagnosis.code, service.code, insurer_id
                )
            decision = decisions[key]
            if decision is None:
                issues.append(
                    rejection(
                        "no_coverage_rule",
                        "No applicable coverage rule. Claim denied by default.",
                        location,
                    )
                )
            elif not decision.is_covered:
                issues.append(
                    rejection(
                        "medical_necessity",
                        f"Medical Necessity Denied: item {item.sequence}, service {service.code}, diagnosis {diagnosis.code}.",
                        location,
                    )
                )
    return issues


async def validate_coverage(
    db: AsyncSession, submission: ClaimSubmission
) -> list[Issue]:
    # SQLAlchemy's greenlet bridge awaits driver I/O inside the existing sync
    # services. It does not run blocking synchronous database I/O on this loop.
    return await db.run_sync(evaluate, submission)
