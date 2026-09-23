"""Insurer overrides first; denial wins conflicts within the selected scope."""

from dataclasses import dataclass
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from models import DiagnosisCode, ServiceCode, DiagnosisServiceRule


@dataclass(frozen=True)
class CoverageDecision:
    is_covered: bool
    insurer_id: int | None


def resolve_coverage(
    db: Session, diagnosis_id: int, service_id: int, insurer_id: int | None
) -> CoverageDecision | None:
    scope = DiagnosisServiceRule.insurer_id.is_(None)
    if insurer_id is not None:
        scope = or_(scope, DiagnosisServiceRule.insurer_id == insurer_id)
    rows = db.execute(
        select(
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
        return None
    # Never depend on database row order or treat explicit False as missing.
    return CoverageDecision(
        is_covered=all(row.is_covered is True for row in selected),
        insurer_id=insurer_id if specific else None,
    )


def resolve_coverage_by_codes(
    db: Session, diagnosis_code: str, service_code: str, insurer_id: int | None
) -> CoverageDecision | None:
    """Resolve existing rule foreign keys; terminology IDs are a different namespace.

    Legacy rows are used only as rule mappings, never as terminology authority.
    No mapping means no applicable rule. Do not create reference rows on requests.
    """
    diagnosis_id = db.scalar(
        select(DiagnosisCode.id).where(DiagnosisCode.code == diagnosis_code)
    )
    service_id = db.scalar(
        select(ServiceCode.id).where(ServiceCode.code == service_code)
    )
    if diagnosis_id is None or service_id is None:
        return None
    return resolve_coverage(db, diagnosis_id, service_id, insurer_id)
