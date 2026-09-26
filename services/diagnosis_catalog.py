"""Read-only ICD-10-AM catalog presence; presence is not release completeness."""

from dataclasses import dataclass
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from diagnosis_systems import ICD10_AM_SYSTEM
from models import NphiesTerminology

MISSING_CATALOG_CODE = "icd10_am_catalog_missing"
MISSING_CATALOG_MESSAGE = "ICD-10-AM terminology catalog is not loaded."


class DiagnosisCatalogMissingError(RuntimeError):
    def __init__(self):
        super().__init__(MISSING_CATALOG_MESSAGE)


@dataclass(frozen=True)
class DiagnosisCatalogReadiness:
    rows: int
    active_rows: int

    @property
    def present(self) -> bool:
        return self.rows > 0


def diagnosis_catalog_readiness(db: Session) -> DiagnosisCatalogReadiness:
    filters = (
        NphiesTerminology.code_system_url == ICD10_AM_SYSTEM,
        NphiesTerminology.is_deleted.is_(False),
        NphiesTerminology.code.is_not(None),
        NphiesTerminology.code != "",
    )
    rows = db.scalar(
        select(func.count()).select_from(NphiesTerminology).where(*filters)
    )
    active = db.scalar(
        select(func.count())
        .select_from(NphiesTerminology)
        .where(*filters, NphiesTerminology.is_active.is_(True))
    )
    return DiagnosisCatalogReadiness(rows=rows, active_rows=active)


def require_diagnosis_catalog(db: Session) -> None:
    if not diagnosis_catalog_readiness(db).present:
        raise DiagnosisCatalogMissingError
