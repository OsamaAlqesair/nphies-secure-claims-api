from database import SessionLocal
from models import Organization, InsuranceCompany

def fix_bupa_relation():
    with SessionLocal.begin() as session:
        org = session.query(Organization).filter_by(identifier_value="INS-BUPA").first()
        
        if not org:
            org = Organization(
                fhir_id="org-bupa",
                identifier_system="http://nphies.sa/license/insurer-license",
                identifier_value="INS-BUPA",
                name="Bupa",
                organization_type="insurer",
                resource={}
            )
            session.add(org)
            session.flush()

        insurer = session.query(InsuranceCompany).filter_by(name="Bupa").first()
        if insurer:
            insurer.organization_id = org.id
            session.add(insurer)

if __name__ == "__main__":
    fix_bupa_relation()
    print("Database patch applied successfully.")