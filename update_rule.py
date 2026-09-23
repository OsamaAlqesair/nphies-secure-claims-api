from database import SessionLocal
from models import DiagnosisServiceRule, DiagnosisCode, InsuranceCompany

def make_bupa_approve():
    with SessionLocal.begin() as session:
        migraine = session.query(DiagnosisCode).filter_by(code="G43").first()
        bupa = session.query(InsuranceCompany).filter_by(name="Bupa").first()
        
        if migraine and bupa:
            rule = session.query(DiagnosisServiceRule).filter_by(
                diagnosis_id=migraine.id,
                insurer_id=bupa.id
            ).first()
            
            if rule:
                rule.is_covered = True
                session.add(rule)
                print("Success: Rule updated. Bupa now covers G43.")
            else:
                print("Error: Rule not found.")

if __name__ == "__main__":
    make_bupa_approve()