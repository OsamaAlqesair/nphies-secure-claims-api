"""Preview or apply an exact coverage rule update; default mode never writes."""

import argparse
from collections.abc import Callable
import json
import logging

from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from models import DiagnosisCode, ServiceCode, InsuranceCompany, DiagnosisServiceRule
from services.coverage_mutations import (
    CoverageMutationError,
    MutationContext,
    lock_coverage_mutations,
    update_rule_coverage,
    validate_reason,
)

logger = logging.getLogger(__name__)


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
    parser.add_argument(
        "--reason", help="Required for --apply; no credentials or patient data."
    )
    args = parser.parse_args(argv)
    if args.apply:
        try:
            args.reason = validate_reason(args.reason)
        except CoverageMutationError as exc:
            parser.error(str(exc))
    changed = False

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
                        lock_coverage_mutations(db)
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
                    changed = update_rule_coverage(
                        db,
                        rule.id,
                        intended,
                        context=MutationContext(
                            reason=args.reason,
                            source="update_rule.py",
                            actor_user_id=None,
                        ),
                    )
            # Reached only after a successful transaction commit.
        print(
            (
                "Success: exact rule update and history committed."
                if changed
                else "Unchanged: no mutation or history created."
            )
            if args.apply
            else "Dry-run: no changes applied."
        )
        return 0
    except (TargetError, CoverageMutationError) as exc:
        print("Failure: " + str(exc))
        return 1
    except Exception as exc:
        if isinstance(exc, SQLAlchemyError):
            # Covers target queries/commit failures; service failures are already logged.
            logger.error(
                "coverage_rule_cli_database_failed exception_type=%s",
                type(exc).__name__,
            )
        print(
            "Failure: database operation did not complete; transaction rolled back. "
            "Exception details suppressed."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
