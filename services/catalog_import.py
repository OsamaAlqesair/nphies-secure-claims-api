"""Reviewed, deterministic reconstructed catalog import; no source/network I/O.

The manifest is code reviewed, not supplied by an administrative artifact. Adding
another edition requires a reviewed manifest; the persistence model already
supports concurrent editions. Import never activates implicitly.
"""

from dataclasses import dataclass
from hashlib import sha256
import json
import re

from sqlalchemy import func, insert, select
from models import TerminologyCatalog, TerminologyCatalogEntry, utcnow
from diagnosis_systems import ICD10_AM_SYSTEM
from services.icd10_am_reconstruction import PARSER_VERSION, RECONSTRUCTION_LABEL

FAMILY = "ICD-10-AM"
ARTIFACT_SCHEMA = "icd10-am-leaf-candidates-v1"
ENTRY_FIELDS = (
    "code",
    "display",
    "display_confidence",
    "source_line",
    "classification",
)


def digest(value):
    return sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ReviewedManifest:
    edition: str
    source_filename: str
    source_sha256: str
    extraction_context: str
    reconstruction_label: str
    parser_version: str
    content_sha256: str
    expected_count: int
    artifact_schema: str = ARTIFACT_SCHEMA
    family: str = FAMILY
    code_system_url: str = ICD10_AM_SYSTEM

    def metadata(self):
        from dataclasses import asdict

        return asdict(self)


TENTH_EDITION = ReviewedManifest(
    "ICD-10-AM Tenth Edition",
    "1015865188-ICD-10-AM-Tabular-List.txt",
    "d437275056f43f7c92478959c815d5881d4c6234d2b59f0e0bc997ece052ec16",
    "EIS eBook, July 2017",
    RECONSTRUCTION_LABEL,
    PARSER_VERSION,
    "975a8f049c4f47dd051c57cb37e799452e6815150da44c19242fe71c90b52d1d",
    16953,
)
REVIEWED_MANIFESTS = (TENTH_EDITION,)


def reconstruction_artifact(result):
    if not all(check.passed for check in result.report.consistency_checks):
        raise ValueError("Reconstruction consistency checks failed.")
    entries = [
        dict(
            code=h.canonical_code,
            display=h.display,
            display_confidence=h.display_confidence.value,
            source_line=h.source_line_start,
            classification=h.classification.value,
        )
        for h in sorted(result.leaves, key=lambda h: h.canonical_code)
    ]
    metadata = dict(
        family=FAMILY,
        code_system_url=ICD10_AM_SYSTEM,
        edition=result.source.edition_label,
        source_filename=result.source.filename,
        source_sha256=result.source.sha256,
        extraction_context=result.source.extraction_context,
        reconstruction_label=result.reconstruction_label,
        parser_version=result.parser_version,
        artifact_schema=ARTIFACT_SCHEMA,
        expected_count=len(entries),
        content_sha256=digest(entries),
    )
    return dict(metadata=metadata, entries=entries)


def verify_artifact(artifact):
    """Return detached canonical data, verified BEFORE opening a transaction."""
    if not isinstance(artifact, dict) or set(artifact) != {"metadata", "entries"}:
        raise ValueError("Invalid catalog artifact schema.")
    metadata, entries = artifact["metadata"], artifact["entries"]
    manifest = next((m for m in REVIEWED_MANIFESTS if m.metadata() == metadata), None)
    if (
        manifest is None
        or not isinstance(entries, list)
        or len(entries) != manifest.expected_count
    ):
        raise ValueError("Catalog is not a reviewed reconstruction.")
    codes = set()
    for row in entries:
        if (
            not isinstance(row, dict)
            or set(row) != set(ENTRY_FIELDS)
            or not isinstance(row["code"], str)
            or not re.fullmatch(r"[A-Z][0-9]{2}(?:\.[0-9]{1,2})?", row["code"])
            or row["code"] in codes
            or row["classification"] != "leaf_candidate"
            or not isinstance(row["display"], str)
            or not row["display"].strip()
            or row["display_confidence"]
            not in ("clean_single_line", "flagged_ambiguous")
            or type(row["source_line"]) is not int
            or row["source_line"] <= 0
        ):
            raise ValueError("Invalid reconstructed leaf candidate.")
        codes.add(row["code"])
    normalized = [dict(row) for row in sorted(entries, key=lambda r: r["code"])]
    if digest(normalized) != manifest.content_sha256:
        raise ValueError("Catalog content differs from the reviewed reconstruction.")
    metadata = manifest.metadata()
    return metadata, normalized, digest(dict(metadata=metadata, entries=normalized))


def _family_lock(db, family):
    if db.get_bind().dialect.name == "postgresql":
        # Stable signed bigint; no Python hash randomization or external locks.
        key = int.from_bytes(sha256(family.encode()).digest()[:8], "big", signed=True)
        db.execute(select(func.pg_advisory_xact_lock(key)))


def import_artifact(artifact, *, session_factory):
    metadata, entries, artifact_hash = verify_artifact(artifact)
    with session_factory.begin() as db:
        _family_lock(db, metadata["family"])
        existing = db.scalar(
            select(TerminologyCatalog).where(
                *(
                    getattr(TerminologyCatalog, field) == metadata[field]
                    for field in (
                        "family",
                        "edition",
                        "source_sha256",
                        "parser_version",
                        "artifact_schema",
                    )
                )
            )
        )
        if existing:
            if existing.artifact_sha256 != artifact_hash or existing.state not in (
                "VALIDATED",
                "ACTIVE",
                "SUPERSEDED",
            ):
                raise ValueError(
                    "Existing catalog identity conflicts or is incomplete."
                )
            return existing.id
        catalog = TerminologyCatalog(**metadata, artifact_sha256=artifact_hash)
        db.add(catalog)
        db.flush()
        db.execute(
            insert(TerminologyCatalogEntry),
            [dict(catalog_id=catalog.id, **row) for row in entries],
        )
        stored = [
            dict(row)
            for row in db.execute(
                select(*(getattr(TerminologyCatalogEntry, f) for f in ENTRY_FIELDS))
                .where(TerminologyCatalogEntry.catalog_id == catalog.id)
                .order_by(TerminologyCatalogEntry.code)
            ).mappings()
        ]
        # Normalize independently of database collation.
        imported_hash = digest(sorted(stored, key=lambda row: row["code"]))
        if (
            len(stored) != metadata["expected_count"]
            or imported_hash != metadata["content_sha256"]
        ):
            raise ValueError("Database import verification failed.")
        catalog.imported_count = len(stored)
        catalog.import_sha256 = imported_hash
        catalog.validated_at = utcnow()
        catalog.state = "VALIDATED"
        db.flush()
        return catalog.id


def activate_catalog(catalog_id, *, session_factory):
    with session_factory.begin() as db:
        catalog = db.get(TerminologyCatalog, catalog_id)
        if catalog is None:
            raise ValueError("Unknown catalog.")
        _family_lock(db, catalog.family)
        db.refresh(catalog, with_for_update=True)
        if not any(
            all(getattr(catalog, k) == v for k, v in m.metadata().items())
            for m in REVIEWED_MANIFESTS
        ):
            raise ValueError("Catalog is not a reviewed reconstruction.")
        if catalog.state == "ACTIVE":
            return catalog.id
        if catalog.state != "VALIDATED":
            raise ValueError("Only a validated catalog can be activated.")
        for old in db.scalars(
            select(TerminologyCatalog)
            .where(
                TerminologyCatalog.family == catalog.family,
                TerminologyCatalog.state == "ACTIVE",
            )
            .with_for_update()
        ):
            old.state = "SUPERSEDED"
        db.flush()
        catalog.state = "ACTIVE"
        catalog.activated_at = utcnow()
        db.flush()
        return catalog.id
