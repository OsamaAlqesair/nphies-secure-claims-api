"""Atomic intake creation and replay; owns the request Session's transaction.

Authentication may have already begun a read transaction on this same Session.
There must be no pending writes from another operation. The first write reserves
the owner's key, before business evaluation. No helper commits independently.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from uuid import UUID, uuid4

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from models import ClaimIntake, ClaimIntakeEvent, ClaimValidationAttempt
from schemas.claim_intake import ClaimIntakeResult
from schemas.fhir_claim import ClaimSubmission
from services.audit import add_event
from services.claim_validation import evaluate_report
from services.claim_report_serialization import (
    REPORT_VERSION,
    report_outcome,
    serialize_report,
)

SUBMISSION_VERSION = "claim-submission-v1"
CREATION_ACTION = "claim_intake.created"


class ClaimIntakeConflict(Exception):
    """The owner already used this key for another normalized request."""


class ClaimIntakeUnavailable(Exception):
    """Controlled persistence failure; never carries database exception text."""


def canonical_input_hash(submission: ClaimSubmission) -> str:
    def normalized(value):
        if isinstance(value, Decimal):
            # Remove insignificant zeros without relying on Decimal context.
            text = format(value, "f")
            return text.rstrip("0").rstrip(".") if "." in text else text
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: normalized(entry) for key, entry in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalized(entry) for entry in value]
        return value

    canonical = json.dumps(
        normalized(submission.model_dump(mode="python")),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return sha256(canonical.encode("ascii")).hexdigest()


def _persisted_result(db: Session, intake: ClaimIntake) -> ClaimIntakeResult:
    attempt = db.execute(
        select(
            ClaimValidationAttempt.result,
            ClaimValidationAttempt.reason,
            ClaimValidationAttempt.operation_outcome_snapshot,
            ClaimValidationAttempt.attempt_no,
        ).where(
            ClaimValidationAttempt.intake_id == intake.id,
            ClaimValidationAttempt.attempt_no == 1,
        )
    ).one()
    created_at = intake.created_at
    if created_at.tzinfo is None:  # SQLite does not retain timezone information.
        created_at = created_at.replace(tzinfo=timezone.utc)
    created_at = created_at.astimezone(timezone.utc)
    return ClaimIntakeResult(
        public_id=intake.public_id,
        result=attempt.result,
        reason=attempt.reason,
        operation_outcome=attempt.operation_outcome_snapshot,
        created_at=created_at,
        attempt_no=attempt.attempt_no,
    )


def rollback(db: Session) -> None:
    try:
        db.rollback()
    except SQLAlchemyError:
        # A failed connection may also reject rollback. Dependency close disposes
        # it; the controlled response must not reveal either database error.
        pass


def create_intake(
    db: Session,
    submission: ClaimSubmission,
    original_json: str,
    idempotency_key: UUID,
    owner_user_id: int,
    request: Request,
) -> tuple[ClaimIntakeResult, bool]:
    """Return the immutable persisted result and whether this call created it."""
    if db.new or db.dirty or db.deleted:
        raise ValueError("Intake creation requires a Session without pending writes.")
    key = str(idempotency_key)
    input_hash = canonical_input_hash(submission)
    try:
        dialect = db.get_bind().dialect.name
        if dialect not in {"postgresql", "sqlite"}:
            raise RuntimeError("Claim intake requires PostgreSQL or isolated SQLite.")
        insert = postgres_insert if dialect == "postgresql" else sqlite_insert
        reserved_id = db.scalar(
            insert(ClaimIntake.__table__)
            .values(
                public_id=str(uuid4()),
                owner_user_id=owner_user_id,
                created_at=datetime.now(timezone.utc),
                idempotency_key=key,
                canonical_input_hash=input_hash,
                original_request_snapshot=original_json,
                validated_submission_snapshot=submission.model_dump(mode="json"),
                schema_version=SUBMISSION_VERSION,
            )
            .on_conflict_do_nothing(index_elements=["owner_user_id", "idempotency_key"])
            .returning(ClaimIntake.id)
        )
        # PostgreSQL READ COMMITTED: the unique-index conflict waits for the
        # winner to commit/roll back. This subsequent statement sees its commit.
        # SQLite's first write serializes writers; no preceding lookup races.
        intake = db.scalars(
            select(ClaimIntake).where(
                ClaimIntake.owner_user_id == owner_user_id,
                ClaimIntake.idempotency_key == key,
            )
        ).one()
        if reserved_id is None:
            if intake.canonical_input_hash != input_hash:
                raise ClaimIntakeConflict()
            result = _persisted_result(db, intake)
            db.rollback()
            return result, False

        report = evaluate_report(db, submission)
        result_code = report.result.value.upper()
        reason = "validation_" + report.result.value
        request_id = getattr(request.state, "audit_request_id", None)
        if request_id is None:
            request_id = str(uuid4())
            request.state.audit_request_id = request_id
        db.add(
            ClaimValidationAttempt(
                intake_id=intake.id,
                attempt_no=1,
                actor_user_id=owner_user_id,
                request_id=request_id,
                result=result_code,
                reason=reason,
                operation_outcome_snapshot=report_outcome(report).model_dump(
                    mode="json", exclude_none=True
                ),
                validation_report_snapshot=serialize_report(report),
                validation_schema_version=REPORT_VERSION,
            )
        )
        db.flush()
        for number, event_type, event_reason, details in (
            (1, "intake.created", "intake_accepted", {}),
            (
                2,
                "validation.completed",
                "validation_completed",
                {"attempt_no": 1, "result": result_code},
            ),
        ):
            db.add(
                ClaimIntakeEvent(
                    intake_id=intake.id,
                    event_no=number,
                    event_type=event_type,
                    actor_user_id=owner_user_id,
                    request_id=request_id,
                    reason=event_reason,
                    details=details,
                )
            )
            db.flush()
        # Existing bounded audit metadata accommodates a public resource reference
        # without adding a schema field or copying any clinical snapshot.
        add_event(
            db,
            request,
            CREATION_ACTION,
            "success",
            "intake:" + intake.public_id,
            201,
            actor_id=owner_user_id,
        )
        db.flush()
        result = _persisted_result(db, intake)
        db.commit()
        return result, True
    except SQLAlchemyError:
        rollback(db)
        raise ClaimIntakeUnavailable() from None
    except BaseException:
        rollback(db)
        raise
