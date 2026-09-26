"""Insert top-level CodeSystem concepts with explicit, commit-aware reporting."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import NphiesTerminology

SOURCE_DIR = Path(__file__).resolve().parent / "nphies_data"


@dataclass
class SeedReport:
    code_system_files_discovered: int = 0
    code_system_files_processed: int = 0
    concepts_discovered: int = 0
    inserted: int = 0
    existing: int = 0
    skipped: int = 0
    failed: int = 0
    file_errors: int = 0
    ignored_files: int = 0

    def print_summary(self) -> None:
        print(
            "Completed with failures." if self.failed else "Completed without failures."
        )
        print(f"CodeSystem files discovered: {self.code_system_files_discovered}")
        print(f"CodeSystem files processed: {self.code_system_files_processed}")
        print(f"Concepts discovered: {self.concepts_discovered}")
        print(f"Inserted (committed): {self.inserted}")
        print(f"Already existing: {self.existing}")
        print(f"Skipped concepts: {self.skipped}")
        print(f"Failed: {self.failed}")
        print(f"File/source/database errors: {self.file_errors}")
        print(f"Ignored non-CodeSystem files: {self.ignored_files}")
        print(
            "Failed counts rejected/rolled-back concepts; an uncountable file or setup failure counts as one."
        )
        if not self.failed and not self.inserted and self.existing:
            print("No new rows: all eligible concepts already exist.")


def seed_terminologies(
    source_dir: Path | str | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> SeedReport:
    """One transaction per CodeSystem; never update existing terminology rows.

    processed counts CodeSystem files completed without file/transaction errors.
    discovered counts identifiable CodeSystem JSON resources, including invalid
    CodeSystem metadata. Malformed JSON cannot be classified as a resource.
    Only top-level concepts are handled; ValueSet references are not imported.
    """
    report = SeedReport()
    source = SOURCE_DIR if source_dir is None else Path(source_dir)

    def fail_source(message: str) -> SeedReport:
        report.failed += 1
        report.file_errors += 1
        print(message)
        report.print_summary()
        return report

    try:
        if not source.is_dir():
            return fail_source("Source directory is missing or is not a directory.")
        files = sorted(source.glob("*.json"), key=lambda path: path.name)
    except OSError:
        return fail_source("Source directory cannot be read.")
    if not files:
        return fail_source("Source directory contains no JSON files.")

    # Lazy initialization keeps isolated tests independent of .env and production.
    if session_factory is None:
        try:
            from database import SessionLocal, engine
            from models import Base

            Base.metadata.create_all(bind=engine)  # Preserve existing setup behavior.
            session_factory = SessionLocal
        except Exception:
            return fail_source("Database initialization failed; details suppressed.")

    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
        except (OSError, UnicodeError, ValueError):
            report.failed += 1
            report.file_errors += 1
            print(
                "A source file could not be read as a JSON resource; details suppressed."
            )
            continue
        if data.get("resourceType") != "CodeSystem":
            report.ignored_files += 1
            continue
        report.code_system_files_discovered += 1
        concepts = data.get("concept", [])
        if isinstance(concepts, list):
            report.concepts_discovered += len(concepts)
        system = data.get("url")
        if (
            not isinstance(system, str)
            or not system.strip()
            or not isinstance(concepts, list)
        ):
            report.failed += max(len(concepts), 1) if isinstance(concepts, list) else 1
            report.file_errors += 1
            print("CodeSystem has an invalid URL or concept array.")
            continue

        candidates = []
        seen = set()
        for concept in concepts:
            if not isinstance(concept, dict):
                report.failed += 1
                continue
            code = concept.get("code")
            if not isinstance(code, str) or not code.strip():
                report.skipped += 1
                continue
            if any(
                concept.get(field) is not None and not isinstance(concept[field], str)
                for field in ("display", "definition")
            ):
                report.failed += 1
                continue
            if code in seen:
                report.skipped += 1
                continue
            seen.add(code)
            candidates.append(concept)
        existing = 0
        try:
            with session_factory() as db:
                pending = 0
                with db.begin():
                    for concept in candidates:
                        # Deleted/inactive rows still exist: do not duplicate or restore.
                        found = db.scalar(
                            select(NphiesTerminology.id)
                            .where(
                                NphiesTerminology.code_system_url == system,
                                NphiesTerminology.code == concept["code"],
                            )
                            .execution_options(include_deleted=True)
                            .limit(1)
                        )
                        if found is not None:
                            existing += 1
                            continue
                        db.add(
                            NphiesTerminology(
                                code_system_url=system,
                                code=concept["code"],
                                display=concept.get("display"),
                                definition=concept.get("definition"),
                            )
                        )
                        pending += 1
                # Transaction context has committed successfully at this point.
                report.inserted += pending
                report.existing += existing
                report.code_system_files_processed += 1
        except Exception:
            # Existing rows were observed, not inserted; rollback does not change
            # them. Pending/unexamined candidates failed. An otherwise empty
            # failure is counted once as a file-level error.
            report.existing += existing
            report.failed += max(len(candidates) - existing, 1)
            report.file_errors += 1
            print(
                "CodeSystem transaction failed and was rolled back; details suppressed."
            )

    if not report.code_system_files_discovered and not report.failed:
        report.failed += 1
        report.file_errors += 1
        print("No CodeSystem resources were found.")
    report.print_summary()
    return report


if __name__ == "__main__":
    raise SystemExit(1 if seed_terminologies().failed else 0)
