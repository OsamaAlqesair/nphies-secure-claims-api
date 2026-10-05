"""Active terminology membership, scoped to clinical code systems."""

from sqlalchemy import select
from sqlalchemy.orm import Session
from models import NphiesTerminology, TerminologyCatalogEntry
from service_systems import SERVICE_SYSTEMS

from diagnosis_systems import ICD10_AM_SYSTEM
from services.diagnosis_catalog import require_diagnosis_catalog

# Backwards-compatible public alias; no second system definition.
DIAGNOSIS_SYSTEM = ICD10_AM_SYSTEM


class AmbiguousTerminologyError(ValueError):
    """The code-only payload cannot select a unique active terminology entry."""


def find_term(
    db: Session, code: str, systems: tuple[str, ...], *, catalog=None
) -> NphiesTerminology | TerminologyCatalogEntry | None:
    if ICD10_AM_SYSTEM in systems and systems != (ICD10_AM_SYSTEM,):
        raise ValueError("Diagnosis membership requires an explicit catalog scope.")
    if systems == (ICD10_AM_SYSTEM,):
        catalog = catalog or require_diagnosis_catalog(db)
        return db.scalar(
            select(TerminologyCatalogEntry).where(
                TerminologyCatalogEntry.catalog_id == catalog.id,
                TerminologyCatalogEntry.code == code,
            )
        )
    # Base's session event excludes soft-deleted rows as well.
    terms = db.scalars(
        select(NphiesTerminology)
        .where(
            NphiesTerminology.code == code,
            NphiesTerminology.code_system_url.in_(systems),
            NphiesTerminology.is_active.is_(True),
        )
        .limit(2)
    ).all()
    if len(terms) > 1:
        raise AmbiguousTerminologyError("Code has multiple active terminology entries.")
    return terms[0] if terms else None
