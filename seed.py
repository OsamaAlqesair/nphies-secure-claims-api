"""Insert development examples without dropping, overwriting, or restoring rows."""

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
import logging
from database import SessionLocal
from models import DiagnosisCode, ServiceCode, InsuranceCompany, DiagnosisServiceRule
from services.coverage_mutations import (
    CoverageMutationError,
    MutationContext,
    create_rule,
    lock_coverage_mutations,
)

logger = logging.getLogger(__name__)
SEED_REASON = "Create missing development coverage example"


def seed_data():
    try:
        with SessionLocal.begin() as session:
            lock_coverage_mutations(session)

            def existing_or_new(model, lookup, defaults):
                obj = session.scalar(
                    select(model)
                    .filter_by(**lookup)
                    .execution_options(include_deleted=True)
                )
                if obj is None:
                    obj = model(**lookup, **defaults)
                    session.add(obj)
                    session.flush()
                if obj.is_deleted:
                    raise CoverageMutationError(
                        "Required development seed mapping is deleted.",
                        code="rule_mapping_deleted",
                    )
                return obj

            cold = existing_or_new(
                DiagnosisCode, {"code": "J00"}, {"description": "Common Cold"}
            )
            migraine = existing_or_new(
                DiagnosisCode, {"code": "G43"}, {"description": "Migraine"}
            )
            ct = existing_or_new(
                ServiceCode, {"code": "70450"}, {"description": "CT Scan Head"}
            )
            insurer = existing_or_new(InsuranceCompany, {"name": "Bupa"}, {})
            for diagnosis, insurer_id, covered in [
                (cold, None, True),
                (migraine, None, True),
                (migraine, insurer.id, False),
            ]:
                # Keep the original include-deleted exact lookup. Any matching
                # row, including duplicates or deleted rows, is left untouched.
                existing = session.scalar(
                    select(DiagnosisServiceRule)
                    .filter_by(
                        diagnosis_id=diagnosis.id,
                        service_id=ct.id,
                        insurer_id=insurer_id,
                    )
                    .execution_options(include_deleted=True)
                )
                if existing is None:
                    create_rule(
                        session,
                        diagnosis.id,
                        ct.id,
                        covered,
                        insurer_id=insurer_id,
                        context=MutationContext(reason=SEED_REASON, source="seed.py"),
                    )
    except SQLAlchemyError as exc:
        logger.error("coverage_seed_failed exception_type=%s", type(exc).__name__)
        raise CoverageMutationError(
            "Development seed failed; transaction rolled back."
        ) from None
    print("Development seed complete; existing rows preserved.")


if __name__ == "__main__":
    try:
        seed_data()
    except CoverageMutationError as exc:
        raise SystemExit("Development seed failed: " + str(exc)) from None
