"""Shared FHIR responses; application reason codes belong in issue.details."""

from fastapi.responses import JSONResponse
from schemas.claim import Issue, OperationOutcome


def generate_rejection(
    diagnostics: str,
    status_code: int = 422,
    *,
    reason: str | None = None,
    code: str = "business-rule",
    expression: list[str] | None = None,
) -> JSONResponse:
    details = (
        None
        if reason is None
        else {
            "coding": [
                {
                    "system": "urn:nphies-secure-claim-api:validation",
                    "code": reason,
                }
            ]
        }
    )
    outcome = OperationOutcome(
        issue=[
            Issue(
                code=code,
                diagnostics=diagnostics,
                details=details,
                expression=expression,
            )
        ]
    )
    return JSONResponse(
        status_code=status_code,
        content=outcome.model_dump(mode="json", exclude_none=True),
        media_type="application/fhir+json",
    )
