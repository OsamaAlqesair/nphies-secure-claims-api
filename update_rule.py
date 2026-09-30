"""Preview or apply an exact coverage rule update; default mode never writes."""

import argparse
from collections.abc import Callable
import json

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from models import DiagnosisCode, ServiceCode, InsuranceCompany, DiagnosisServiceRule


class TargetError(ValueError):
    """Safe, fixed diagnostic for a missing or ambiguous target."""


def positive_id(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("Insurer ID must be positive.")
    return result


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Callable[[], Session] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnosis-code", required=True)
    parser.add_argument("--service-code", required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--insurer-id", type=positive_id)
    scope.add_argument("--global", dest="global_scope", action="store_true")
    parser.add_argument("--covered", choices=("true", "false"), required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    try:
        if session_factory is None:
            from database import SessionLocal

            session_factory = SessionLocal
        with session_factory() as db:
            # Context manager rolls back any selection, flush or commit failure.
            with db.begin():
                dialect = db.get_bind().dialect.name
                if dialect == "postgresql":
                    if args.apply:
                        # No uniqueness constraint exists. Serialize writes during this
                        # short maintenance transaction, including concurrent inserts.
                        db.execute(
                            text(
                                "LOCK TABLE diagnosis_codes, service_codes, "
                                "insurance_companies, diagnosis_service_rules "
                                "IN SHARE ROW EXCLUSIVE MODE"
                            )
                        )
                    else:
                        db.execute(text("SET TRANSACTION READ ONLY"))

                def mapping(model, condition, label):
                    matches = db.scalars(
                        select(model.id)
                        .where(condition, model.is_deleted.is_(False))
                        .limit(2)
                    ).all()
                    if len(matches) != 1:
                        raise TargetError(label + " mapping missing or ambiguous.")
                    return matches[0]

                diagnosis_id = mapping(
                    DiagnosisCode,
                    DiagnosisCode.code == args.diagnosis_code,
                    "Diagnosis",
                )
                service_id = mapping(
                    ServiceCode, ServiceCode.code == args.service_code, "Service"
                )
                if args.insurer_id is not None:
                    mapping(
                        InsuranceCompany,
                        InsuranceCompany.id == args.insurer_id,
                        "Insurer",
                    )
                insurer_filter = (
                    DiagnosisServiceRule.insurer_id.is_(None)
                    if args.global_scope
                    else DiagnosisServiceRule.insurer_id == args.insurer_id
                )
                matches = db.scalars(
                    select(DiagnosisServiceRule)
                    .where(
                        DiagnosisServiceRule.diagnosis_id == diagnosis_id,
                        DiagnosisServiceRule.service_id == service_id,
                        insurer_filter,
                        DiagnosisServiceRule.is_deleted.is_(False),
                    )
                    .limit(2)
                ).all()
                if not matches:
                    raise TargetError("No matching non-deleted rule.")
                if len(matches) != 1:
                    raise TargetError(
                        "Multiple matching non-deleted rules; update refused."
                    )
                rule = matches[0]
                intended = args.covered == "true"
                # JSON escaping prevents control characters from forging output lines.
                print(
                    "Target: "
                    + json.dumps(
                        {
                            "diagnosis_code": args.diagnosis_code,
                            "service_code": args.service_code,
                            "scope": (
                                "global" if args.global_scope else "insurer-specific"
                            ),
                            "insurer_id": args.insurer_id,
                            "current_is_covered": rule.is_covered,
                            "intended_is_covered": intended,
                        },
                        ensure_ascii=True,
                    )
                )
                if args.apply:
                    rule.is_covered = intended
            # Reached only after a successful transaction commit.
        print(
            "Success: exact rule update committed."
            if args.apply
            else "Dry-run: no changes applied."
        )
        return 0
    except TargetError as exc:
        print("Failure: " + str(exc))
        return 1
    except Exception:
        print(
            "Failure: database operation did not complete; transaction rolled back. "
            "Exception details suppressed."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
