"""Authenticated FHIR Claim pre-validation with asynchronous database access."""

from typing import Annotated
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from async_database import get_async_db
from auth import claim_access
from schemas.fhir_claim import ClaimSubmission
from schemas.claim import OperationOutcome, Issue
from services.outcomes import generate_rejection
from services.audit import add_event
from services.claim_validation import validate_coverage


class FHIRResponse(JSONResponse):
    media_type = "application/fhir+json"


router = APIRouter(prefix="/api/v1/claims", tags=["Claim pre-validation"])


@router.post(
    "/pre-validate",
    response_model=OperationOutcome,
    response_class=FHIRResponse,
    dependencies=[Depends(claim_access)],
    responses={
        401: {
            "model": OperationOutcome,
            "description": "Bearer authentication required",
        },
        403: {
            "model": OperationOutcome,
            "description": "Provider or admin role required",
        },
        422: {
            "model": OperationOutcome,
            "description": "Structural, financial or coverage validation failed",
        },
        503: {
            "model": OperationOutcome,
            "description": "Database validation unavailable",
        },
    },
)
async def pre_validate_claim(
    submission: ClaimSubmission,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_async_db)],
) -> FHIRResponse:
    """Validate all linked pairs; this operation neither submits nor stores a Claim."""
    try:
        issues = await validate_coverage(db, submission)
        status = 422 if issues else 200
        # Keep the audit helper inside the same async-to-sync bridge as queries.
        await db.run_sync(
            add_event,
            request,
            "claim.validation",
            "failure" if issues else "success",
            issues[0].details["coding"][0]["code"] if issues else "approved",
            status,
        )
        # An approval is never returned if its audit record failed to persist.
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        return FHIRResponse(
            status_code=503,
            content=OperationOutcome(
                issue=[
                    Issue(
                        severity="error",
                        code="transient",
                        diagnostics="Claim validation is temporarily unavailable. Retry later.",
                    )
                ]
            ).model_dump(mode="json", exclude_none=True),
        )
    if len(issues) == 1:
        issue = issues[0]
        return generate_rejection(
            issue.diagnostics,
            reason=issue.details["coding"][0]["code"],
            code=issue.code,
            expression=issue.expression,
        )
    if not issues:
        issues = [
            Issue(
                severity="information",
                code="informational",
                diagnostics="Structural, financial and coverage pre-validation passed. This is not payer authorization.",
            )
        ]
    return FHIRResponse(
        status_code=status,
        content=OperationOutcome(issue=issues).model_dump(
            mode="json", exclude_none=True
        ),
    )
