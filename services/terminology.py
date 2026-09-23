"""Active terminology membership, scoped to clinical code systems."""

from sqlalchemy import select
from sqlalchemy.orm import Session
from models import NphiesTerminology

DIAGNOSIS_SYSTEM = "http://hl7.org/fhir/sid/icd-10-am"
SERVICE_SYSTEMS = (
    "http://nphies.sa/terminology/CodeSystem/services",
    "http://nphies.sa/terminology/CodeSystem/procedures",
    "http://nphies.sa/terminology/CodeSystem/laboratory",
    "http://nphies.sa/terminology/CodeSystem/imaging",
    "http://nphies.sa/terminology/CodeSystem/oral-health-ip",
    "http://nphies.sa/terminology/CodeSystem/oral-health-op",
    "http://nphies.sa/terminology/CodeSystem/medication-codes",
)


class AmbiguousTerminologyError(ValueError):
    """The code-only payload cannot select a unique active terminology entry."""


def find_term(
    db: Session, code: str, systems: tuple[str, ...]
) -> NphiesTerminology | None:
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
