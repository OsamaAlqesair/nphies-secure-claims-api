"""Read-only legacy SQLite import into an EMPTY, migrated PostgreSQL database."""

import argparse
from pathlib import Path
import sqlite3
import logging
from sqlalchemy import select, func, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from database import engine
from models import Base
from services.coverage_mutations import (
    CoverageMutationError,
    MutationContext,
    import_rule,
    lock_coverage_mutations,
)

logger = logging.getLogger(__name__)
IMPORT_REASON = "Import legacy coverage rule into destination"

LEGACY_TABLES = (
    "diagnosis_codes",
    "service_codes",
    "insurance_companies",
    "diagnosis_service_rules",
)


def import_legacy(source: Path, target):
    source = source.resolve(strict=True)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as legacy:
        legacy.row_factory = sqlite3.Row
        try:
            with target.begin() as connection:
                return _import_destination(legacy, target, connection)
        except SQLAlchemyError as exc:
            logger.error(
                "coverage_legacy_import_failed exception_type=%s", type(exc).__name__
            )
            raise CoverageMutationError(
                "Legacy import failed; transaction rolled back."
            ) from None


def _import_destination(legacy, target, connection):
    # Join the existing Connection transaction without acquiring commit ownership.
    # Closing this Session does not commit or roll back the outer transaction.
    with Session(bind=connection, join_transaction_mode="rollback_only") as db:
        db.begin()
        # Serializes importer executions; app writes must be stopped during import.
        if target.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(74839501)"))
        lock_coverage_mutations(db)
        for name in LEGACY_TABLES:
            table = Base.metadata.tables[name]
            if connection.scalar(select(func.count()).select_from(table)):
                raise RuntimeError(
                    f"Target {name} is not empty; refusing to overwrite data."
                )
        counts = {}
        for name in LEGACY_TABLES:
            table = Base.metadata.tables[name]
            rows = [dict(row) for row in legacy.execute(f"SELECT * FROM {name}")]
            for row in rows:
                if "is_covered" in row:
                    row["is_covered"] = bool(row["is_covered"])
                if "is_deleted" in row:
                    row["is_deleted"] = bool(row["is_deleted"])
            if rows:
                if name == "diagnosis_service_rules":
                    context = MutationContext(
                        reason=IMPORT_REASON, source="migrate_sqlite.py"
                    )
                    for row in rows:
                        timestamps = {
                            key: row[key]
                            for key in ("created_at", "updated_at")
                            if key in row
                        }
                        import_rule(
                            db,
                            row.get("id"),
                            row.get("diagnosis_id"),
                            row.get("service_id"),
                            row.get("is_covered", False),
                            insurer_id=row.get("insurer_id"),
                            is_deleted=row.get("is_deleted", False),
                            context=context,
                            **timestamps,
                        )
                else:
                    connection.execute(table.insert(), rows)
            counts[name] = len(rows)
            if connection.scalar(select(func.count()).select_from(table)) != len(rows):
                raise RuntimeError(f"Count mismatch in {name}; rolling back.")
            if target.dialect.name == "postgresql":
                connection.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), COALESCE((SELECT MAX(id) FROM {name}), 1), EXISTS(SELECT 1 FROM {name}))"
                    )
                )
        return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).with_name("rules_engine.db")
    )
    args = parser.parse_args()
    if engine.dialect.name != "postgresql":
        raise SystemExit("The import target must be PostgreSQL.")
    try:
        print(import_legacy(args.source, engine))
    except CoverageMutationError as exc:
        raise SystemExit("Legacy import failed: " + str(exc)) from None
