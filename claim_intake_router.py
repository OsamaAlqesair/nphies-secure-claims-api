"""Authenticated intake creation; structural validation precedes all writes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from auth import bearer, claim_access, current_user
from claim_router import FHIRResponse
from database import get_db
from models import User
from schemas.claim import Issue, OperationOutcome
from schemas.claim_intake import ClaimIntakeResult
from schemas.fhir_claim import ClaimSubmission
from services.claim_intake import (
    ClaimIntakeConflict,
    ClaimIntakeUnavailable,
    create_intake,
    rollback,
)

router = APIRouter(prefix="/api/v1/claim-intakes", tags=["Claim intake"])


def unavailable_response() -> FHIRResponse:
    return FHIRResponse(
        status_code=503,
        content=OperationOutcome(
            issue=[
                Issue(
                    code="transient",
                    diagnostics="Claim intake is temporarily unavailable. Retry later.",
                )
            ]
        ).model_dump(mode="json", exclude_none=True),
    )


def intake_owner(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    try:
        return claim_access(current_user(request, credentials, db))
    except SQLAlchemyError:
        rollback(db)
        raise ClaimIntakeUnavailable() from None


async def original_json(request: Request) -> str:
    # FastAPI has cached the same bytes used to build ClaimSubmission. Accept
    # UTF-8 JSON text, without a BOM, so TEXT is lossless on both supported DBs.
    try:
        text = (await request.body()).decode("utf-8")
        if text.startswith("\ufeff") or "\x00" in text:
            raise ValueError()
        return text
    except (UnicodeError, ValueError):
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("body",),
                    "msg": "UTF-8 JSON text is required.",
                }
            ]
        ) from None


@router.post(
    "",
    response_model=ClaimIntakeResult,
    response_model_exclude_none=True,
    status_code=201,
    responses={
        200: {"model": ClaimIntakeResult, "description": "Immutable idempotent replay"},
        409: {
            "model": OperationOutcome,
            "description": "Key already used for different input",
        },
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
            "description": "Invalid request or idempotency key",
        },
        503: {
            "model": OperationOutcome,
            "description": "Intake persistence unavailable",
        },
    },
)
def post_claim_intake(
    submission: ClaimSubmission,
    request: Request,
    response: Response,
    owner: Annotated[User, Depends(intake_owner)],
    db: Annotated[Session, Depends(get_db)],
    raw_json: Annotated[str, Depends(original_json)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ClaimIntakeResult | FHIRResponse:
    try:
        result, created = create_intake(
            db, submission, raw_json, idempotency_key, owner.id, request
        )
    except ClaimIntakeConflict:
        return FHIRResponse(
            status_code=409,
            content=OperationOutcome(
                issue=[
                    Issue(
                        code="business-rule",
                        diagnostics="Idempotency-Key was already used for a different request.",
                    )
                ]
            ).model_dump(mode="json", exclude_none=True),
        )
    except ClaimIntakeUnavailable:
        return unavailable_response()
    response.status_code = 201 if created else 200
    return result
