import json
import glob
import os
from sqlalchemy.orm import Session
from database import SessionLocal, engine
from models import NphiesTerminology, Base

def seed_terminologies():
    print("Starting data seeding process...")
    
    Base.metadata.create_all(bind=engine)
    
    db: Session = SessionLocal()
    
    folder_path = "nphies_data"
    json_files = glob.glob(os.path.join(folder_path, "*.json"))
    
    total_added = 0

    for file_path in json_files:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                if data.get('resourceType') == 'CodeSystem':
                    url = data.get('url', 'Unknown URL')
                    concepts = data.get('concept', [])
                    
                    if not concepts:
                        continue
                        
                    for concept in concepts:
                        code = concept.get('code')
                        display = concept.get('display')
                        definition = concept.get('definition')
                        
                        exists = db.query(NphiesTerminology).filter_by(
                            code_system_url=url, code=code
                        ).first()
                        
                        if not exists and code:
                            new_term = NphiesTerminology(
                                code_system_url=url,
                                code=code,
                                display=display,
                                definition=definition
                            )
                            db.add(new_term)
                            total_added += 1
                            
                    db.commit()
                    print(f"Added codes for: {url}")
                    
        except Exception as e:
            print(f"Error in file {file_path}: {e}")
            db.rollback()

    db.close()
    print(f"\nSuccess! Added {total_added} new codes to the database.")

if __name__ == "__main__":
    seed_terminologies()