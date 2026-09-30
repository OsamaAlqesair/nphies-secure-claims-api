"""Import an explicitly supplied, approved local ICD-10-AM JSON dataset.

No catalog is bundled or downloaded. Default mode is dry-run. Schema migrations
must already be applied to the chosen database; this script never creates tables.
"""

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from diagnosis_systems import ICD10AMSystem, ICD10_AM_SYSTEM
from models import NphiesTerminology


class ApprovedConcept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Annotated[
        str, Field(strict=True, pattern=r"^[A-Z][0-9]{2}(?:\.[0-9]{1,2})?$")
    ]
    display: Annotated[
        str, Field(strict=True, min_length=1, max_length=2000, pattern=r"\S")
    ]
    active: bool = Field(strict=True)


class ApprovedDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system: ICD10AMSystem
    version: (
        Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r"\S")]
        | None
    ) = None
    concepts: list[ApprovedConcept] = Field(min_length=1)


@dataclass
class ImportReport:
    inserted: int = 0
    existing: int = 0
    would_insert: int = 0
    failed: int = 0
    diagnostic: str = ""

    def print_summary(self, dry_run: bool) -> None:
        print("Mode: dry-run" if dry_run else "Mode: apply")
        print(
            f"Inserted: {self.inserted}; existing: {self.existing}; "
            f"would insert: {self.would_insert}; failed: {self.failed}"
        )
        if self.diagnostic:
            print(self.diagnostic)


def load_approved_dataset(path: Path) -> ApprovedDataset:
    # Reject duplicate JSON keys instead of silently taking the final value.
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key.")
            result[key] = value
        return result

    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
    dataset = ApprovedDataset.model_validate(data)
    if len({entry.code for entry in dataset.concepts}) != len(dataset.concepts):
        raise ValueError("Duplicate diagnosis codes in dataset.")
    return dataset


def import_dataset(
    path: Path,
    *,
    dry_run: bool = True,
    session_factory: Callable[[], Session] | None = None,
) -> ImportReport:
    report = ImportReport()
    try:
        dataset = load_approved_dataset(path)
    except (OSError, UnicodeError, ValueError, ValidationError):
        report.failed = 1
        report.diagnostic = "Dataset rejected: check file, system identity, unique codes, display and boolean active status."
        return report

    # Metadata must not be silently lost, even when explicitly supplied as null.
    if not dry_run and "version" in dataset.model_fields_set:
        report.failed = len(dataset.concepts)
        report.diagnostic = (
            "Version persistence is not yet supported. "
            "Datasets containing version metadata are accepted for dry-run inspection only."
        )
        return report

    try:
        if session_factory is None:
            from database import SessionLocal

            session_factory = SessionLocal
        with session_factory() as db:
            with db.begin():
                if db.get_bind().dialect.name == "postgresql":
                    if dry_run:
                        db.execute(text("SET TRANSACTION READ ONLY"))
                    else:
                        # Serialize runs of this importer without changing catalog data.
                        db.execute(
                            text("SELECT pg_advisory_xact_lock(hashtext(:system))"),
                            {"system": ICD10_AM_SYSTEM},
                        )
                rows = db.scalars(
                    select(NphiesTerminology)
                    .where(NphiesTerminology.code_system_url == ICD10_AM_SYSTEM)
                    .execution_options(include_deleted=True)
                ).all()
                indexed = {}
                for row in rows:
                    indexed.setdefault(row.code, []).append(row)
                pending = []
                for concept in dataset.concepts:
                    matches = indexed.get(concept.code, [])
                    if not matches:
                        pending.append(concept)
                    elif (
                        len(matches) == 1
                        and not matches[0].is_deleted
                        and matches[0].display == concept.display
                        and matches[0].is_active == concept.active
                    ):
                        report.existing += 1
                    else:
                        report.failed += 1
                if report.failed:
                    report.diagnostic = "Existing record conflict; no rows inserted, updated, restored or deleted."
                    return report
                report.would_insert = len(pending)
                if not dry_run:
                    db.add_all(
                        [
                            NphiesTerminology(
                                code_system_url=ICD10_AM_SYSTEM,
                                code=concept.code,
                                display=concept.display,
                                is_active=concept.active,
                            )
                            for concept in pending
                        ]
                    )
            # Commit completed before recording successful insertions.
            if not dry_run:
                report.inserted = len(pending)
                report.would_insert = 0
    except IntegrityError:
        report.inserted = report.would_insert = 0
        report.failed = max(len(dataset.concepts) - report.existing, 1)
        report.diagnostic = "Terminology insertion constraint conflict; entire import rolled back. No changes applied."
    except Exception:
        report.would_insert = 0
        report.inserted = 0
        report.failed = max(len(dataset.concepts) - report.existing, 1)
        report.diagnostic = "Database import failed. Verify connection and required migrations; exception details suppressed."
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "file", type=Path, help="Explicit path to an approved local dataset."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and compare without writes (default).",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Insert approved missing entries into the configured database.",
    )
    args = parser.parse_args(argv)
    report = import_dataset(args.file, dry_run=not args.apply)
    report.print_summary(dry_run=not args.apply)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
