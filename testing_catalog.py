"""Explicit SYNTHETIC catalogs for isolated integration fixtures only.

These helpers never read clinical source data or run from application code. Old
tests can keep their coverage examples while adopting immutable catalog versions.
"""

from uuid import uuid4
from sqlalchemy import select
from models import (
    NphiesTerminology,
    TerminologyCatalog,
    TerminologyCatalogEntry,
    utcnow,
)
from diagnosis_systems import ICD10_AM_SYSTEM
from services.catalog_import import digest, ENTRY_FIELDS
from services import catalog_import


def retire_catalog(db):
    for catalog in db.scalars(
        select(TerminologyCatalog).where(TerminologyCatalog.state == "ACTIVE")
    ):
        catalog.state = "SUPERSEDED"
    db.flush()


def synthetic_catalog(db, entries=None):
    # Call explicitly after fixture edits: no runtime hooks/trust of legacy rows.
    db.flush()
    if entries is None:
        entries = [
            dict(code=r.code, display=r.display or "SYNTHETIC TEST ONLY")
            for r in db.scalars(
                select(NphiesTerminology).where(
                    NphiesTerminology.code_system_url == ICD10_AM_SYSTEM,
                    NphiesTerminology.is_active.is_(True),
                )
            )
        ]
    entries = [
        dict(
            code=r["code"],
            display=r.get("display") or "SYNTHETIC TEST ONLY",
            display_confidence=r.get("display_confidence", "clean_single_line"),
            source_line=r.get("source_line", i + 1),
            classification="leaf_candidate",
        )
        for i, r in enumerate(sorted(entries, key=lambda r: r["code"]))
    ]
    retire_catalog(db)
    if not entries:
        # An unrelated member keeps readiness true for unknown diagnosis tests.
        entries = [
            dict(
                code="Z99.9",
                display="SYNTHETIC TEST ONLY",
                display_confidence="clean_single_line",
                source_line=1,
                classification="leaf_candidate",
            )
        ]
    content = digest(entries)
    catalog = TerminologyCatalog(
        family="ICD-10-AM",
        code_system_url=ICD10_AM_SYSTEM,
        edition="SYNTHETIC TEST ONLY " + uuid4().hex,
        source_filename="synthetic-test.json",
        source_sha256=digest("synthetic"),
        extraction_context="synthetic test",
        reconstruction_label="SYNTHETIC TEST ONLY leaf candidates",
        parser_version="test-v1",
        artifact_schema="test-v1",
        artifact_sha256=digest(dict(entries=entries)),
        content_sha256=content,
        expected_count=len(entries),
    )
    db.add(catalog)
    db.flush()
    manifest = catalog_import.ReviewedManifest(
        **{
            name: getattr(catalog, name)
            for name in catalog_import.ReviewedManifest.__dataclass_fields__
        }
    )
    catalog_import.REVIEWED_MANIFESTS = (*catalog_import.REVIEWED_MANIFESTS, manifest)
    db.add_all(TerminologyCatalogEntry(catalog_id=catalog.id, **r) for r in entries)
    db.flush()
    catalog.state = "VALIDATED"
    catalog.imported_count = len(entries)
    catalog.import_sha256 = content
    catalog.validated_at = utcnow()
    db.flush()
    catalog.state = "ACTIVE"
    catalog.activated_at = utcnow()
    db.flush()
    return catalog.id
