"""Authorized immutable history reads; no evaluation, flush, commit or audit."""

from datetime import datetime, timezone
from functools import wraps
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from models import ClaimIntake, ClaimIntakeEvent, ClaimValidationAttempt, User
from schemas.claim_intake import (
    ClaimIntakeDetail,
    ClaimIntakeList,
    ClaimIntakeSummary,
    ClaimIntakeTimelineItem,
    ClaimValidationHistoryItem,
)
from services.claim_intake import ClaimIntakeUnavailable

STATES = {
    "PASSED": "validated",
    "FAILED": "validation_failed",
    "UNAVAILABLE": "validation_unavailable",
}


class ClaimIntakeNotFound(Exception):
    """Identical outcome for a missing or inaccessible public parent."""


def historical_read(operation):
    @wraps(operation)
    def read(db: Session, *args, **kwargs):
        try:
            with db.no_autoflush:
                return operation(db, *args, **kwargs)
        except (SQLAlchemyError, ValidationError):
            raise ClaimIntakeUnavailable() from None

    return read


def _scope(user: User):
    return () if user.role == "admin" else (ClaimIntake.owner_user_id == user.id,)


def _utc(value):
    if not isinstance(value, datetime):
        raise ClaimIntakeUnavailable()
    if value.tzinfo is None:  # SQLite stores the UTC value without its timezone.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _summary(parent, attempt):
    if attempt is None or attempt["result"] not in STATES:
        raise ClaimIntakeUnavailable()
    if attempt["reason"] != "validation_" + attempt["result"].lower():
        raise ClaimIntakeUnavailable()
    return ClaimIntakeSummary(
        public_id=parent["public_id"],
        created_at=_utc(parent["created_at"]),
        state=STATES[attempt["result"]],
        latest_attempt_no=attempt["attempt_no"],
        latest_result=attempt["result"],
        latest_reason=attempt["reason"],
    )


def _parent(db, user, public_id, *, submission=False):
    columns = [ClaimIntake.id, ClaimIntake.public_id, ClaimIntake.created_at]
    if submission:
        columns.extend(
            [ClaimIntake.validated_submission_snapshot, ClaimIntake.schema_version]
        )
    parent = (
        db.execute(
            select(*columns).where(
                ClaimIntake.public_id == str(public_id), *_scope(user)
            )
        )
        .mappings()
        .first()
    )
    if parent is None:
        raise ClaimIntakeNotFound()
    return parent


def _latest(db, intake_id):
    return (
        db.execute(
            select(
                ClaimValidationAttempt.attempt_no,
                ClaimValidationAttempt.result,
                ClaimValidationAttempt.reason,
            )
            .where(ClaimValidationAttempt.intake_id == intake_id)
            .order_by(ClaimValidationAttempt.attempt_no.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )


@historical_read
def list_intakes(db: Session, user: User, limit: int, offset: int) -> ClaimIntakeList:
    scope = _scope(user)
    total = db.scalar(select(func.count()).select_from(ClaimIntake).where(*scope))
    # Aggregate by parent, then join its authoritative highest attempt number.
    # Restrict the aggregate for providers as well as the outer page/count.
    latest = (
        select(
            ClaimValidationAttempt.intake_id,
            func.max(ClaimValidationAttempt.attempt_no).label("attempt_no"),
        )
        .join(ClaimIntake, ClaimIntake.id == ClaimValidationAttempt.intake_id)
        .where(*scope)
        .group_by(ClaimValidationAttempt.intake_id)
        .subquery()
    )
    rows = (
        db.execute(
            select(
                ClaimIntake.public_id,
                ClaimIntake.created_at,
                ClaimValidationAttempt.attempt_no,
                ClaimValidationAttempt.result,
                ClaimValidationAttempt.reason,
            )
            .select_from(ClaimIntake)
            .outerjoin(latest, latest.c.intake_id == ClaimIntake.id)
            .outerjoin(
                ClaimValidationAttempt,
                and_(
                    ClaimValidationAttempt.intake_id == ClaimIntake.id,
                    ClaimValidationAttempt.attempt_no == latest.c.attempt_no,
                ),
            )
            .where(*scope)
            .order_by(ClaimIntake.created_at.desc(), ClaimIntake.id.desc())
            .limit(limit)
            .offset(offset)
        )
        .mappings()
        .all()
    )
    return ClaimIntakeList(
        items=[_summary(row, row) for row in rows],
        limit=limit,
        offset=offset,
        total=total,
    )


@historical_read
def intake_detail(db: Session, user: User, public_id: UUID) -> ClaimIntakeDetail:
    parent = _parent(db, user, public_id, submission=True)
    summary = _summary(parent, _latest(db, parent["id"]))
    if parent["schema_version"] != "claim-submission-v1":
        raise ClaimIntakeUnavailable()
    return ClaimIntakeDetail(
        **summary.model_dump(),
        submission=parent["validated_submission_snapshot"],
    )


@historical_read
def validation_history(
    db: Session, user: User, public_id: UUID
) -> list[ClaimValidationHistoryItem]:
    # Strict parent-first authorization: no attempt query for inaccessible IDs.
    parent = _parent(db, user, public_id)
    rows = (
        db.execute(
            select(
                ClaimValidationAttempt.attempt_no,
                ClaimValidationAttempt.result,
                ClaimValidationAttempt.reason,
                ClaimValidationAttempt.occurred_at,
                ClaimValidationAttempt.operation_outcome_snapshot,
                ClaimValidationAttempt.validation_report_snapshot,
                ClaimValidationAttempt.validation_schema_version,
            )
            .where(ClaimValidationAttempt.intake_id == parent["id"])
            .order_by(ClaimValidationAttempt.attempt_no.asc())
        )
        .mappings()
        .all()
    )
    if not rows:
        raise ClaimIntakeUnavailable()
    items = []
    for row in rows:
        outcome = row["operation_outcome_snapshot"]
        if (
            row["validation_schema_version"] != "claim-validation-report-v1"
            or not isinstance(outcome, dict)
            or outcome.get("resourceType") != "OperationOutcome"
        ):
            raise ClaimIntakeUnavailable()
        items.append(
            ClaimValidationHistoryItem(
                attempt_no=row["attempt_no"],
                result=row["result"],
                reason=row["reason"],
                occurred_at=_utc(row["occurred_at"]),
                operation_outcome=outcome,
                validation_report=row["validation_report_snapshot"],
            )
        )
    return items


@historical_read
def intake_timeline(
    db: Session, user: User, public_id: UUID
) -> list[ClaimIntakeTimelineItem]:
    # Resolve exactly the same authorized parent before querying event history.
    parent = _parent(db, user, public_id)
    rows = (
        db.execute(
            select(
                ClaimIntakeEvent.event_no,
                ClaimIntakeEvent.event_type,
                ClaimIntakeEvent.occurred_at,
                ClaimIntakeEvent.reason,
                ClaimIntakeEvent.details,
            )
            .where(ClaimIntakeEvent.intake_id == parent["id"])
            .order_by(ClaimIntakeEvent.event_no.asc())
        )
        .mappings()
        .all()
    )
    return [
        ClaimIntakeTimelineItem(
            event_no=row["event_no"],
            event_type=row["event_type"],
            occurred_at=_utc(row["occurred_at"]),
            reason=row["reason"],
            details=row["details"],
        )
        for row in rows
    ]
