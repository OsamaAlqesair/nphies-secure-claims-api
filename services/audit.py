"""Minimal audit facts only: never accept request/response bodies or credentials."""

from uuid import uuid4
from fastapi import Request
from sqlalchemy.orm import Session
from models import AuditLog
from database import get_db


def add_event(
    db: Session,
    request: Request,
    action: str,
    outcome: str,
    reason: str,
    status: int,
    actor_id: int | None = None,
) -> None:
    request_id = getattr(request.state, "audit_request_id", None)
    if request_id is None:
        request_id = str(uuid4())
        request.state.audit_request_id = request_id
    db.add(
        AuditLog(
            actor_user_id=(
                actor_id
                if actor_id is not None
                else getattr(request.state, "audit_actor_id", None)
            ),
            action=action,
            outcome=outcome,
            reason=reason,
            http_status=status,
            request_id=request_id,
            endpoint=request.url.path[:128],
            client_ip=request.client.host[:64] if request.client else None,
        )
    )


def record_failure(request: Request, action: str, reason: str, status: int) -> None:
    # Exception handlers run after request dependencies unwind. Use a new session
    # so failure records survive rollback; honor the same DB override in tests.
    factory = request.app.dependency_overrides.get(get_db, get_db)
    dependency = factory()
    try:
        db = next(dependency)
        add_event(db, request, action, "failure", reason, status)
        db.commit()
    finally:
        dependency.close()
