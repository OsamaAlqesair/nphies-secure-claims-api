"""Read-only legacy SQLite import into an EMPTY, migrated PostgreSQL database."""

import argparse
from pathlib import Path
import sqlite3
from sqlalchemy import select, func, text
from database import engine
from models import Base

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
        with target.begin() as connection:
            # Serializes importer executions; app writes must be stopped during import.
            if target.dialect.name == "postgresql":
                connection.execute(text("SELECT pg_advisory_xact_lock(74839501)"))
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
                    connection.execute(table.insert(), rows)
                counts[name] = len(rows)
                if connection.scalar(select(func.count()).select_from(table)) != len(
                    rows
                ):
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
    print(import_legacy(args.source, engine))
