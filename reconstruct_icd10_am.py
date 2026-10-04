"""Development-only ICD-10-AM reconstruction dry run; no DB/import/app startup.

Usage: python reconstruct_icd10_am.py --source <tabular.txt> --report-only
Optional --output-dir writes candidate/rejection/report JSON to explicit paths.
These artifacts remain reconstructed candidates with unresolved display flags.
They are not the approved dataset accepted by import_icd10_am.py.
"""

import argparse
import json
from pathlib import Path

from services.icd10_am_reconstruction import (
    ReconstructionError,
    normalized_json,
    parse_file,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Explicit local UTF-8 Tabular TXT path.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--report-only",
        action="store_true",
        help="Print structured counts/provenance without file writes (default).",
    )
    mode.add_argument(
        "--output-dir",
        type=Path,
        help="Write three candidate JSON artifacts; existing files are never overwritten.",
    )
    args = parser.parse_args(argv)
    try:
        result = parse_file(args.source)
        if args.output_dir is not None:
            data = result.to_dict()
            documents = {
                "candidate_catalog.json": {
                    "reconstruction_label": result.reconstruction_label,
                    "source": data["source"],
                    "candidates": [
                        h
                        for h in data["headings"]
                        if h["classification"] == "leaf_candidate"
                    ],
                },
                "rejected_candidates.json": {
                    "source": data["source"],
                    "rejections": data["rejections"],
                },
                "reconstruction_report.json": {
                    **result.summary(),
                    "report": data["report"],
                    "parent_candidates": [
                        h
                        for h in data["headings"]
                        if h["classification"] == "nonassignable_parent_candidate"
                    ],
                },
            }
            paths = tuple(args.output_dir / name for name in documents)
            if any(
                path.resolve() == args.source.resolve() or path.exists()
                for path in paths
            ):
                raise ValueError(
                    "Output would overwrite a source or existing artifact."
                )
            args.output_dir.mkdir(parents=True, exist_ok=True)
            for path, document in zip(paths, documents.values()):
                with path.open("x", encoding="utf-8", newline="\n") as output:
                    output.write(normalized_json(document))
        print(normalized_json(result.summary()), end="")
        counts = result.report.counts
        return (
            2
            if counts.malformed_code_column_tokens
            or counts.ambiguous_heading_identities
            else 0
        )
    except (OSError, UnicodeError, ValueError) as exc:
        error = {"error": str(exc), "database_access": False}
        if isinstance(exc, ReconstructionError):
            error["diagnostic_lines"] = [row.source_line for row in exc.diagnostics]
            error["duplicate_codes"] = [
                group.canonical_code for group in exc.duplicates
            ]
        print(json.dumps(error, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
