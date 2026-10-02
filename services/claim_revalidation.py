"""Atomic append-only revalidation; owns a clean request Session after auth.

PostgreSQL serializes each intake with its parent row lock. SQLite ends only
the request's read-only auth transaction, then acquires its writer reservation
before resolving the parent or reading history. No helper commits independently.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import Request
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, SessionTransactionOrigin

from models import ClaimIntake, ClaimIntakeEvent, ClaimValidationAttempt, User
from schemas.claim_intake import ClaimRevalidationResult
from schemas.fhir_claim import ClaimSubmission
from services.audit import add_event
from services.claim_intake import ClaimIntakeUnavailable, SUBMISSION_VERSION, rollback
from services.claim_intake_reads import ClaimIntakeNotFound
from services.claim_report_serialization import (
    REPORT_VERSION,
    report_outcome,
    serialize_report,
)
from services.claim_validation import evaluate_report

REVALIDATION_ACTION = "claim_intake.revalidated"


def _result(public_id, attempt):
    outcome = attempt.operation_outcome_snapshot
    if (
        attempt.validation_schema_version != REPORT_VERSION
        or not isinstance(outcome, dict)
        or outcome.get("resourceType") != "OperationOutcome"
    ):
        raise ClaimIntakeUnavailable()
    occurred_at = attempt.occurred_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    return ClaimRevalidationResult(
        public_id=public_id,
        attempt_no=attempt.attempt_no,
        result=attempt.result,
        reason=attempt.reason,
        operation_outcome=outcome,
        occurred_at=occurred_at.astimezone(timezone.utc),
    )


def revalidate_intake(
    db: Session,
    actor: User,
    public_id: UUID,
    idempotency_key: UUID,
    request: Request,
) -> tuple[ClaimRevalidationResult, bool]:
    """Append one attempt/event/audit or replay one immutable historical result."""
    transaction = db.get_transaction()
    if (
        db.new
        or db.dirty
        or db.deleted
        or db.in_nested_transaction()
        or (
            transaction is not None
            and transaction.origin != SessionTransactionOrigin.AUTOBEGIN
        )
    ):
        raise ClaimIntakeUnavailable()
    actor_id, admin = actor.id, actor.role == "admin"
    key = str(idempotency_key)
    try:
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite":
            # Authentication read is complete and has no pending writes. Ending
            # it avoids upgrading a stale SQLite read snapshot into a writer.
            db.rollback()
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        elif dialect != "postgresql":
            raise ClaimIntakeUnavailable()

        with db.no_autoflush:
            parent_query = select(
                ClaimIntake.id,
                ClaimIntake.public_id,
                ClaimIntake.validated_submission_snapshot,
                ClaimIntake.schema_version,
            ).where(ClaimIntake.public_id == str(public_id))
            if not admin:
                parent_query = parent_query.where(ClaimIntake.owner_user_id == actor_id)
            if dialect == "postgresql":
                parent_query = parent_query.with_for_update()
            parent = db.execute(parent_query).one_or_none()
            if parent is None:
                raise ClaimIntakeNotFound()

            # No child history is queried before the authorized parent is locked.
            previous = db.execute(
                select(
                    ClaimValidationAttempt.attempt_no,
                    ClaimValidationAttempt.result,
                    ClaimValidationAttempt.reason,
                    ClaimValidationAttempt.operation_outcome_snapshot,
                    ClaimValidationAttempt.occurred_at,
                    ClaimValidationAttempt.validation_schema_version,
                ).where(
                    ClaimValidationAttempt.intake_id == parent.id,
                    ClaimValidationAttempt.revalidation_idempotency_key == key,
                )
            ).one_or_none()
            if previous is not None:
                result = _result(parent.public_id, previous)
                db.rollback()  # Release the serialization lock without writes.
                return result, False

            if parent.schema_version != SUBMISSION_VERSION:
                raise ClaimIntakeUnavailable()
            submission = ClaimSubmission.model_validate(
                parent.validated_submission_snapshot
            )
            attempt_no = db.scalar(
                select(func.max(ClaimValidationAttempt.attempt_no)).where(
                    ClaimValidationAttempt.intake_id == parent.id
                )
            )
            event_no = db.scalar(
                select(func.max(ClaimIntakeEvent.event_no)).where(
                    ClaimIntakeEvent.intake_id == parent.id
                )
            )
            if attempt_no is None or attempt_no < 1 or event_no is None or event_no < 2:
                raise ClaimIntakeUnavailable()
            attempt_no += 1
            event_no += 1
            report = evaluate_report(db, submission)
            result_code = report.result.value.upper()
            request_id = getattr(request.state, "audit_request_id", None)
            if request_id is None:
                request_id = str(uuid4())
                request.state.audit_request_id = request_id
            occurred_at = datetime.now(timezone.utc)
            attempt = ClaimValidationAttempt(
                intake_id=parent.id,
                attempt_no=attempt_no,
                actor_user_id=actor_id,
                request_id=request_id,
                occurred_at=occurred_at,
                result=result_code,
                reason="validation_" + report.result.value,
                operation_outcome_snapshot=report_outcome(report).model_dump(
                    mode="json", exclude_none=True
                ),
                validation_report_snapshot=serialize_report(report),
                validation_schema_version=REPORT_VERSION,
                revalidation_idempotency_key=key,
            )
            db.add(attempt)
            db.flush()
            db.add(
                ClaimIntakeEvent(
                    intake_id=parent.id,
                    event_no=event_no,
                    event_type="validation.completed",
                    actor_user_id=actor_id,
                    request_id=request_id,
                    occurred_at=occurred_at,
                    reason="validation_completed",
                    details={"attempt_no": attempt_no, "result": result_code},
                )
            )
            db.flush()
            add_event(
                db,
                request,
                REVALIDATION_ACTION,
                "success",
                "intake:" + parent.public_id,
                201,
                actor_id=actor_id,
            )
            db.flush()
            result = _result(parent.public_id, attempt)
        db.commit()
        return result, True
    except (SQLAlchemyError, ValidationError):
        rollback(db)
        raise ClaimIntakeUnavailable() from None
    except BaseException:
        rollback(db)
        raise
