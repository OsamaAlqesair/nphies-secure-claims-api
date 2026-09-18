from schemas.claim import Claim
from decimal import Decimal

def map_to_fhir_claim(claim: Claim) -> dict:
    fhir_claim = {
        "resourceType": "Claim",
        "status": "active",
        "use": "claim",
        "patient": {
            "identifier": {
                "system": "http://nphies.sa/identifier/nationalid",
                "value": claim.patient_national_id
            }
        },
        "diagnosis": [
            {
                "sequence": 1,
                "diagnosisCodeableConcept": {
                    "coding": [
                        {
                            "system": "http://hl7.org/fhir/sid/icd-10-am",
                            "code": claim.diagnosis_code
                        }
                    ]
                }
            }
        ],
        "item": [],
        "total": {
            "value": float(Decimal(claim.total_claim_amount)),
            "currency": "SAR"
        }
    }

    for index, item in enumerate(claim.items, start=1):
        net_value = Decimal(str(item.quantity)) * Decimal(item.unit_price)
        
        fhir_item = {
            "sequence": index,
            "productOrService": {
                "text": item.description
            },
            "quantity": {
                "value": item.quantity
            },
            "unitPrice": {
                "value": float(Decimal(item.unit_price)),
                "currency": "SAR"
            },
            "net": {
                "value": float(net_value),
                "currency": "SAR"
            }
        }
        fhir_claim["item"].append(fhir_item)

    return fhir_claim