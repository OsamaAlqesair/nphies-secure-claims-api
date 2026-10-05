"""Explicit local reviewed reconstruction import and separate activation."""

import argparse
from pathlib import Path
from services.catalog_import import (
    reconstruction_artifact,
    import_artifact,
    activate_catalog,
    verify_artifact,
)
from services.icd10_am_reconstruction import parse_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("import-source")
    prepare.add_argument("source", type=Path)
    prepare.add_argument(
        "--apply",
        action="store_true",
        help="Import validated catalog; does not activate.",
    )
    activate = commands.add_parser("activate")
    activate.add_argument("catalog_id", type=int)
    args = parser.parse_args()
    try:
        if args.command == "import-source":
            artifact = reconstruction_artifact(parse_file(args.source))
            metadata, entries, artifact_hash = verify_artifact(artifact)
            print(
                f"Reviewed candidates: {len(entries)}; artifact SHA-256: {artifact_hash}"
            )
            if not args.apply:
                return 0
            from database import SessionLocal

            catalog_id = import_artifact(artifact, session_factory=SessionLocal)
            print(f"Catalog {catalog_id}: imported/validated; activation is separate.")
        else:
            from database import SessionLocal

            catalog_id = activate_catalog(args.catalog_id, session_factory=SessionLocal)
            print(f"Catalog {catalog_id}: active.")
    except Exception:
        print(
            "Catalog operation failed; source/identity, migration and connection must be checked. Details suppressed."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
