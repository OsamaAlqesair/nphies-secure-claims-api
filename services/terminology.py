"""Active terminology membership, scoped to clinical code systems."""

from sqlalchemy import select
from sqlalchemy.orm import Session
from models import NphiesTerminology
from service_systems import SERVICE_SYSTEMS

DIAGNOSIS_SYSTEM = "http://hl7.org/fhir/sid/icd-10-am"


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
