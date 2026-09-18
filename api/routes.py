from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
import secrets
from decimal import Decimal

from schemas.claim import Claim, ValidationResult
from services.fhir_mapper import map_to_fhir_claim

router = APIRouter()

@router.post("/validate-claim", response_model=ValidationResult)
async def validate_claim_post(claim: Claim):
    calculated_total = sum(Decimal(str(item.quantity)) * Decimal(item.unit_price) for item in claim.items)
    
    if Decimal(claim.total_claim_amount) != calculated_total:
        error_response = {
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "invalid",
                    "diagnostics": f"Total claim amount ({claim.total_claim_amount}) does not match the sum of items ({calculated_total})."
                }
            ]
        }
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_response,
            media_type="application/fhir+json"
        )

    fhir_payload = map_to_fhir_claim(claim)
    
    return ValidationResult(
        status="validated",
        validation_token=f"sim_{secrets.token_urlsafe(32)}",
        simulated=True,
        fhir_claim=fhir_payload
    )