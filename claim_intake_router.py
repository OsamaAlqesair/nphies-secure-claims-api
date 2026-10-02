"""Authenticated intake creation and authorized immutable historical reads."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from auth import bearer, claim_access, current_user
from claim_router import FHIRResponse
from database import get_db
from models import User
from schemas.claim import Issue, OperationOutcome
from schemas.claim_intake import (
    CanonicalPublicID,
    ClaimIntakeDetail,
    ClaimIntakeList,
    ClaimIntakeResult,
    ClaimIntakeTimelineItem,
    ClaimRevalidationResult,
    ClaimValidationHistoryItem,
)
from schemas.fhir_claim import ClaimSubmission
from services.claim_intake import (
    ClaimIntakeConflict,
    ClaimIntakeUnavailable,
    create_intake,
    rollback,
)
from services import claim_intake_reads, claim_revalidation

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


READ_ERRORS = {
    status: {
        "description": description,
        "content": {
            "application/fhir+json": {"schema": OperationOutcome.model_json_schema()}
        },
    }
    for status, description in {
        401: "Bearer authentication required",
        403: "Provider or admin role required",
        404: "Intake not found or inaccessible",
        422: "Invalid public UUID or pagination",
        503: "Historical retrieval unavailable",
    }.items()
}


def historical_response(db, operation, *args):
    try:
        return operation(db, *args)
    except claim_intake_reads.ClaimIntakeNotFound:
        return FHIRResponse(
            status_code=404,
            content=OperationOutcome(
                issue=[
                    Issue(
                        code="not-found",
                        diagnostics="Claim intake not found.",
                    )
                ]
            ).model_dump(mode="json", exclude_none=True),
        )
    except ClaimIntakeUnavailable:
        rollback(db)
        return unavailable_response()


@router.get("", response_model=ClaimIntakeList, responses=READ_ERRORS)
def get_claim_intakes(
    user: Annotated[User, Depends(intake_owner)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return historical_response(db, claim_intake_reads.list_intakes, user, limit, offset)


@router.get("/{public_id}", response_model=ClaimIntakeDetail, responses=READ_ERRORS)
def get_claim_intake(
    public_id: CanonicalPublicID,
    user: Annotated[User, Depends(intake_owner)],
    db: Annotated[Session, Depends(get_db)],
):
    return historical_response(db, claim_intake_reads.intake_detail, user, public_id)


@router.get(
    "/{public_id}/validations",
    response_model=list[ClaimValidationHistoryItem],
    responses=READ_ERRORS,
)
def get_claim_validation_history(
    public_id: CanonicalPublicID,
    user: Annotated[User, Depends(intake_owner)],
    db: Annotated[Session, Depends(get_db)],
):
    return historical_response(
        db, claim_intake_reads.validation_history, user, public_id
    )


@router.get(
    "/{public_id}/timeline",
    response_model=list[ClaimIntakeTimelineItem],
    responses=READ_ERRORS,
)
def get_claim_intake_timeline(
    public_id: CanonicalPublicID,
    user: Annotated[User, Depends(intake_owner)],
    db: Annotated[Session, Depends(get_db)],
):
    return historical_response(db, claim_intake_reads.intake_timeline, user, public_id)


async def no_revalidation_body(
    request: Request,
    user: Annotated[User, Depends(intake_owner)],
) -> None:
    if await request.body():
        raise RequestValidationError(
            [
                {
                    "type": "extra_forbidden",
                    "loc": ("body",),
                    "msg": "No request body is permitted.",
                }
            ]
        ) from None


@router.post(
    "/{public_id}/validations",
    response_model=ClaimRevalidationResult,
    status_code=201,
    dependencies=[Depends(no_revalidation_body)],
    responses={
        **READ_ERRORS,
        200: {
            "model": ClaimRevalidationResult,
            "description": "Immutable revalidation replay",
        },
        422: {
            **READ_ERRORS[422],
            "description": "Invalid public UUID, key, or request body",
        },
    },
)
def post_claim_revalidation(
    public_id: CanonicalPublicID,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(intake_owner)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ClaimRevalidationResult | FHIRResponse:
    outcome = historical_response(
        db,
        claim_revalidation.revalidate_intake,
        user,
        public_id,
        idempotency_key,
        request,
    )
    if isinstance(outcome, FHIRResponse):
        return outcome
    result, created = outcome
    response.status_code = 201 if created else 200
    return result
