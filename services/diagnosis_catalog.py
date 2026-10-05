"""Cheap authoritative catalog readiness; legacy row presence is irrelevant."""

from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.orm import Session
from diagnosis_systems import ICD10_AM_SYSTEM
from models import TerminologyCatalog

MISSING_CATALOG_CODE = "icd10_am_catalog_missing"
MISSING_CATALOG_MESSAGE = "ICD-10-AM terminology catalog is not loaded."


class DiagnosisCatalogMissingError(RuntimeError):
    def __init__(self):
        super().__init__(MISSING_CATALOG_MESSAGE)


@dataclass(frozen=True)
class CatalogIdentity:
    id: int
    family: str
    edition: str
    source_sha256: str
    parser_version: str
    artifact_schema: str
    artifact_sha256: str
    content_sha256: str
    reconstruction_label: str


@dataclass(frozen=True)
class DiagnosisCatalogReadiness:
    rows: int = 0
    active_rows: int = 0
    catalog: CatalogIdentity | None = None

    @property
    def present(self) -> bool:
        return self.catalog is not None


def diagnosis_catalog_readiness(db: Session) -> DiagnosisCatalogReadiness:
    row = db.scalar(
        select(TerminologyCatalog).where(
            TerminologyCatalog.family == "ICD-10-AM",
            TerminologyCatalog.code_system_url == ICD10_AM_SYSTEM,
            TerminologyCatalog.state == "ACTIVE",
            TerminologyCatalog.imported_count == TerminologyCatalog.expected_count,
            TerminologyCatalog.import_sha256 == TerminologyCatalog.content_sha256,
            TerminologyCatalog.validated_at.is_not(None),
            TerminologyCatalog.activated_at.is_not(None),
        )
    )
    if row is None:
        return DiagnosisCatalogReadiness()
    from services.catalog_import import REVIEWED_MANIFESTS

    if not any(
        all(getattr(row, k) == v for k, v in manifest.metadata().items())
        for manifest in REVIEWED_MANIFESTS
    ):
        return DiagnosisCatalogReadiness()
    identity = CatalogIdentity(
        **{name: getattr(row, name) for name in CatalogIdentity.__dataclass_fields__}
    )
    return DiagnosisCatalogReadiness(row.imported_count, row.imported_count, identity)


def require_diagnosis_catalog(db: Session) -> CatalogIdentity:
    readiness = diagnosis_catalog_readiness(db)
    if not readiness.present:
        raise DiagnosisCatalogMissingError
    return readiness.catalog
