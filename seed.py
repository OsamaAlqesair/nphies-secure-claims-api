"""Insert development examples without dropping, overwriting, or restoring rows."""

from sqlalchemy import select
from database import SessionLocal
from models import DiagnosisCode, ServiceCode, InsuranceCompany, DiagnosisServiceRule


def seed_data():
    with SessionLocal.begin() as session:

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
            (cold, None, False),
            (migraine, None, True),
            (migraine, insurer.id, False),
        ]:
            existing_or_new(
                DiagnosisServiceRule,
                dict(
                    diagnosis_id=diagnosis.id, service_id=ct.id, insurer_id=insurer_id
                ),
                {"is_covered": covered},
            )
    print("Development seed complete; existing rows preserved.")


if __name__ == "__main__":
    seed_data()
