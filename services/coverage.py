"""Insurer overrides first; denial wins conflicts within the selected scope."""

from dataclasses import dataclass
from enum import Enum
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from models import DiagnosisCode, ServiceCode, DiagnosisServiceRule


class CoverageStatus(str, Enum):
    APPROVED = "approved"
    DENIED = "denied"
    DIAGNOSIS_MAPPING_MISSING = "diagnosis_mapping_missing"
    SERVICE_MAPPING_MISSING = "service_mapping_missing"
    NO_APPLICABLE_RULE = "no_applicable_rule"


class CoverageScope(str, Enum):
    INSURER_SPECIFIC = "insurer-specific"
    GLOBAL = "global"
    NONE = "none"


@dataclass(frozen=True)
class CoverageDecision:
    """Internal resolution metadata; never serialize this object as an API response."""

    status: CoverageStatus
    diagnosis_id: int | None
    service_id: int | None
    matched_rule_ids: tuple[int, ...] = ()
    selected_scope: CoverageScope = CoverageScope.NONE
    insurer_id: int | None = None

    @property
    def is_covered(self) -> bool:
        return self.status is CoverageStatus.APPROVED


# Application reason codes, not NPHIES-standard codes. Preserve explicit denial's
# existing public code; mapping failures now have their own actionable reasons.
COVERAGE_FAILURES: dict[CoverageStatus, tuple[str, str]] = {
    CoverageStatus.DIAGNOSIS_MAPPING_MISSING: (
        "diagnosis_mapping_missing",
        "Diagnosis coverage mapping is missing.",
    ),
    CoverageStatus.SERVICE_MAPPING_MISSING: (
        "service_mapping_missing",
        "Service coverage mapping is missing.",
    ),
    CoverageStatus.NO_APPLICABLE_RULE: (
        "no_coverage_rule",
        "No applicable coverage rule. Claim denied by default.",
    ),
    CoverageStatus.DENIED: ("medical_necessity", "Medical Necessity Denied."),
}


def resolve_coverage(
    db: Session, diagnosis_id: int, service_id: int, insurer_id: int | None
) -> CoverageDecision:
    scope = DiagnosisServiceRule.insurer_id.is_(None)
    if insurer_id is not None:
        scope = or_(scope, DiagnosisServiceRule.insurer_id == insurer_id)
    rows = db.execute(
        select(
            DiagnosisServiceRule.id,
            DiagnosisServiceRule.insurer_id,
            DiagnosisServiceRule.is_covered,
        ).where(
            DiagnosisServiceRule.diagnosis_id == diagnosis_id,
            DiagnosisServiceRule.service_id == service_id,
            scope,
        )
    ).all()
    specific = [
        row for row in rows if insurer_id is not None and row.insurer_id == insurer_id
    ]
    selected = specific or [row for row in rows if row.insurer_id is None]
    if not selected:
        # The scoped query deliberately excludes other insurers. Distinguishing
        # their rules from total absence would require extra/broader queries.
        return CoverageDecision(
            CoverageStatus.NO_APPLICABLE_RULE, diagnosis_id, service_id
        )
    # Never depend on database row order or treat explicit False as missing.
    return CoverageDecision(
        status=(
            CoverageStatus.APPROVED
            if all(row.is_covered is True for row in selected)
            else CoverageStatus.DENIED
        ),
        diagnosis_id=diagnosis_id,
        service_id=service_id,
        matched_rule_ids=tuple(sorted(row.id for row in selected)),
        selected_scope=(
            CoverageScope.INSURER_SPECIFIC if specific else CoverageScope.GLOBAL
        ),
        insurer_id=insurer_id if specific else None,
    )


def resolve_coverage_by_codes(
    db: Session, diagnosis_code: str, service_code: str, insurer_id: int | None
) -> CoverageDecision:
    """Resolve existing rule foreign keys; terminology IDs are a different namespace.

    Legacy rows are used only as rule mappings, never as terminology authority.
    Missing mappings are explicit failures. Do not create rows on requests.
    If both mappings are absent, diagnosis failure takes diagnostic precedence.
    """
    diagnosis_id = db.scalar(
        select(DiagnosisCode.id).where(DiagnosisCode.code == diagnosis_code)
    )
    service_id = db.scalar(
        select(ServiceCode.id).where(ServiceCode.code == service_code)
    )
    if diagnosis_id is None:
        return CoverageDecision(
            CoverageStatus.DIAGNOSIS_MAPPING_MISSING, diagnosis_id, service_id
        )
    if service_id is None:
        return CoverageDecision(
            CoverageStatus.SERVICE_MAPPING_MISSING, diagnosis_id, service_id
        )
    return resolve_coverage(db, diagnosis_id, service_id, insurer_id)
