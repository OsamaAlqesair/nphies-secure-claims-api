"""Caller-owned, transactionally audited coverage updates and lifecycle mutations."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
import unicodedata
import logging
import sqlite3

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from services.coverage_write_guards import (
    CoverageMutationError,
    _audited_service,
    _authorize,
    _execute_session_text,
)

from models import (
    CoverageRuleHistory,
    DiagnosisCode,
    DiagnosisServiceRule,
    InsuranceCompany,
    ServiceCode,
    User,
)

logger = logging.getLogger(__name__)


def validate_reason(reason: str | None) -> str:
    if not isinstance(reason, str):
        raise CoverageMutationError("Reason must contain 1-500 nonblank characters.")
    reason = reason.strip()
    if not reason or len(reason) > 500:
        raise CoverageMutationError("Reason must contain 1-500 nonblank characters.")
    if any(unicodedata.category(char).startswith("C") for char in reason):
        raise CoverageMutationError("Reason must not contain control characters.")
    return reason


@dataclass(frozen=True)
class MutationContext:
    reason: str
    source: Literal[
        "update_rule.py", "coverage_mutations.py", "seed.py", "migrate_sqlite.py"
    ] = "update_rule.py"
    # Only trusted application callers may supply an authenticated actor.
    # The current CLI always leaves this NULL.
    actor_user_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", validate_reason(self.reason))
        if self.source not in (
            "update_rule.py",
            "coverage_mutations.py",
            "seed.py",
            "migrate_sqlite.py",
        ):
            raise CoverageMutationError("Unsupported mutation source.")
        if self.actor_user_id is not None and (
            type(self.actor_user_id) is not int or self.actor_user_id <= 0
        ):
            raise CoverageMutationError(
                "Invalid trusted actor reference.", code="rule_actor_invalid"
            )


def lock_coverage_mutations(db: Session) -> None:
    """Share the CLI's existing lock order; caller owns the transaction.

    All supported mutations take the same table lock before any row lock, avoiding
    lock-order inversion between direct service callers and exact-target CLI reads.
    SQLite is only used for isolated behavioral tests, not concurrency guarantees.
    """
    if not db.in_transaction():
        raise CoverageMutationError("An active caller-owned transaction is required.")
    if db.get_bind().dialect.name == "postgresql":
        try:
            _execute_session_text(db, "coverage_lock")
        except SQLAlchemyError as exc:
            # Only controlled codes and exception types; never exception text/tracebacks.
            logger.error(
                "coverage_mutation_lock_failed exception_type=%s", type(exc).__name__
            )
            raise CoverageMutationError(
                "Coverage mutation lock could not be acquired."
            ) from None


@_audited_service
def update_rule_coverage(
    db: Session, rule_id: int, is_covered: bool, *, context: MutationContext
) -> bool:
    """Return whether coverage changed; caller MUST own and finish the transaction.

    No commits or independent audit sessions. Any error must escape the caller's
    transaction block so both writes roll back. No identity reassignment is exposed.
    PostgreSQL row locking ensures the before-state is read after prior updates.
    The CLI additionally locks tables for its exact-target ambiguity check.
    """
    if not db.in_transaction():
        raise CoverageMutationError("An active caller-owned transaction is required.")
    if type(is_covered) is not bool:
        raise CoverageMutationError("Coverage must be a boolean.")
    if any(
        isinstance(obj, DiagnosisServiceRule)
        and obj.id == rule_id
        and db.is_modified(obj, include_collections=True)
        for obj in db.dirty
    ):
        raise CoverageMutationError("Rule has pending changes; audited update refused.")
    # Reject other pending rule writes even on a no-op update, before refreshing.
    for obj in list(db.new) + list(db.dirty) + list(db.deleted):
        if isinstance(obj, DiagnosisServiceRule) and (
            obj in db.new or obj in db.deleted or db.is_modified(obj)
        ):
            raise CoverageMutationError(
                "Rule has pending changes; audited update refused.",
                code="rule_pending_changes",
            )
    try:
        lock_coverage_mutations(db)
        with db.no_autoflush:
            rule = db.scalar(
                select(DiagnosisServiceRule)
                .where(
                    DiagnosisServiceRule.id == rule_id,
                    DiagnosisServiceRule.is_deleted.is_(False),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if rule is None:
                raise CoverageMutationError("Non-deleted rule not found.")
            diagnosis = db.get(DiagnosisCode, rule.diagnosis_id)
            service = db.get(ServiceCode, rule.service_id)
            insurer = (
                db.get(InsuranceCompany, rule.insurer_id)
                if rule.insurer_id is not None
                else None
            )
            if (
                diagnosis is None
                or diagnosis.is_deleted
                or service is None
                or service.is_deleted
                or (
                    rule.insurer_id is not None
                    and (insurer is None or insurer.is_deleted)
                )
            ):
                raise CoverageMutationError("Non-deleted rule mappings are required.")
            if context.actor_user_id is not None:
                actor = db.get(User, context.actor_user_id)
                if actor is None or actor.is_deleted:
                    raise CoverageMutationError("Trusted actor record not found.")
            if rule.is_covered == is_covered:
                return False
            history = CoverageRuleHistory(
                rule_id=rule.id,
                action="UPDATE",
                diagnosis_id=rule.diagnosis_id,
                service_id=rule.service_id,
                insurer_id=rule.insurer_id,
                diagnosis_code=diagnosis.code,
                service_code=service.code,
                insurer_name=insurer.name if insurer is not None else None,
                old_is_covered=rule.is_covered,
                new_is_covered=is_covered,
                old_is_deleted=False,
                new_is_deleted=False,
                actor_user_id=context.actor_user_id,
                source=context.source,
                reason=context.reason,
            )
            with _authorize(db, rule, "UPDATE", history):
                rule.is_covered = is_covered
                db.add(history)
                db.flush()
        return True
    except SQLAlchemyError as exc:
        logger.error(
            "coverage_mutation_update_failed exception_type=%s", type(exc).__name__
        )
        raise CoverageMutationError(
            "Coverage update failed; caller must roll back the transaction."
        ) from None


def _prepare_lifecycle(db: Session, context: MutationContext) -> MutationContext:
    if not db.in_transaction():
        raise CoverageMutationError("An active caller-owned transaction is required.")
    if not isinstance(context, MutationContext):
        raise CoverageMutationError("A normalized mutation context is required.")
    context = MutationContext(
        reason=context.reason,
        source=context.source,
        actor_user_id=context.actor_user_id,
    )
    # Pending
    # coverage/mapping changes could otherwise be flushed without their own audit
    # or overwritten by populate_existing after the lock. Already flushed writes
    # from earlier audited calls in this transaction remain supported.
    relevant = (DiagnosisServiceRule, DiagnosisCode, ServiceCode, InsuranceCompany)
    for obj in list(db.new) + list(db.dirty) + list(db.deleted):
        if isinstance(obj, relevant) or (
            isinstance(obj, User)
            and context.actor_user_id is not None
            and obj.id == context.actor_user_id
        ):
            if obj in db.new or obj in db.deleted or db.is_modified(obj):
                raise CoverageMutationError(
                    "Rule or mapping has pending changes; audited mutation refused.",
                    code="rule_pending_changes",
                )
    return context


def _load_including_deleted(db: Session, model, row_id: int):
    return db.scalar(
        select(model)
        .where(model.id == row_id)
        .with_for_update()
        .execution_options(include_deleted=True, populate_existing=True)
    )


def _lifecycle_mappings(
    db: Session,
    diagnosis_id: int,
    service_id: int,
    insurer_id: int | None,
    *,
    require_current: bool,
):
    diagnosis = _load_including_deleted(db, DiagnosisCode, diagnosis_id)
    service = _load_including_deleted(db, ServiceCode, service_id)
    insurer = (
        _load_including_deleted(db, InsuranceCompany, insurer_id)
        if insurer_id is not None
        else None
    )
    mappings = [diagnosis, service] + ([insurer] if insurer_id is not None else [])
    if any(mapping is None for mapping in mappings):
        raise CoverageMutationError(
            "Rule mapping record not found.", code="rule_mapping_missing"
        )
    if require_current and any(mapping.is_deleted for mapping in mappings):
        raise CoverageMutationError(
            "Non-deleted rule mappings are required.", code="rule_mapping_deleted"
        )
    return diagnosis, service, insurer


def _validate_lifecycle_actor(db: Session, context: MutationContext) -> None:
    if context.actor_user_id is not None:
        actor = _load_including_deleted(db, User, context.actor_user_id)
        if actor is None or actor.is_deleted:
            raise CoverageMutationError(
                "Trusted actor record not found.", code="rule_actor_invalid"
            )


def _lifecycle_history(
    rule: DiagnosisServiceRule,
    mappings,
    action: str,
    old_is_deleted: bool | None,
    context: MutationContext,
) -> CoverageRuleHistory:
    diagnosis, service, insurer = mappings
    return CoverageRuleHistory(
        rule_id=rule.id,
        action=action,
        diagnosis_id=rule.diagnosis_id,
        service_id=rule.service_id,
        insurer_id=rule.insurer_id,
        diagnosis_code=diagnosis.code,
        service_code=service.code,
        insurer_name=insurer.name if insurer is not None else None,
        old_is_covered=None if action == "CREATE" else rule.is_covered,
        new_is_covered=rule.is_covered,
        old_is_deleted=old_is_deleted,
        new_is_deleted=(
            rule.is_deleted if action == "CREATE" else action == "SOFT_DELETE"
        ),
        actor_user_id=context.actor_user_id,
        source=context.source,
        reason=context.reason,
    )


_CURRENT_IDENTITY_INDEXES = frozenset(
    (
        "uq_diagnosis_service_rule_current_global",
        "uq_diagnosis_service_rule_current_insurer",
    )
)


def _current_peer(db, diagnosis_id, service_id, insurer_id):
    scope = (
        DiagnosisServiceRule.insurer_id.is_(None)
        if insurer_id is None
        else DiagnosisServiceRule.insurer_id == insurer_id
    )
    return db.scalar(
        select(DiagnosisServiceRule.id)
        .where(
            DiagnosisServiceRule.diagnosis_id == diagnosis_id,
            DiagnosisServiceRule.service_id == service_id,
            DiagnosisServiceRule.is_deleted.is_(False),
            scope,
        )
        .limit(1)
        .execution_options(include_deleted=True)
    )


def _is_identity_violation(exc):
    if not isinstance(exc, IntegrityError):
        return False
    original = exc.orig
    if getattr(original, "sqlstate", None) == "23505":
        return (
            getattr(getattr(original, "diag", None), "constraint_name", None)
            in _CURRENT_IDENTITY_INDEXES
        )
    # SQLite reports column signatures rather than these index names. Never echo
    # its message; unknown signatures (including other UNIQUE keys) stay generic.
    return (
        isinstance(original, sqlite3.IntegrityError)
        and getattr(original, "sqlite_errorcode", None)
        == sqlite3.SQLITE_CONSTRAINT_UNIQUE
        and str(original)
        in (
            "UNIQUE constraint failed: diagnosis_service_rules.diagnosis_id, "
            "diagnosis_service_rules.service_id",
            "UNIQUE constraint failed: diagnosis_service_rules.diagnosis_id, "
            "diagnosis_service_rules.service_id, diagnosis_service_rules.insurer_id",
        )
    )


def _identity_conflict(action):
    return CoverageMutationError(
        "Another current rule has the same identity.", code=f"rule_{action}_conflict"
    )


def _lifecycle_failure(action: str, exc: SQLAlchemyError) -> CoverageMutationError:
    if action in ("create", "restore", "import") and _is_identity_violation(exc):
        return _identity_conflict(action)
    logger.error(
        "coverage_mutation_%s_failed exception_type=%s", action, type(exc).__name__
    )
    return CoverageMutationError(
        "Coverage mutation failed; caller must roll back the transaction."
    )


@_audited_service
def create_rule(
    db: Session,
    diagnosis_id: int,
    service_id: int,
    is_covered: bool,
    *,
    insurer_id: int | None = None,
    context: MutationContext,
) -> DiagnosisServiceRule:
    """Create a current rule and CREATE event only when its identity is available.

    Caller MUST let errors escape its transaction block and finish the transaction.
    The generated ID is available on the returned rule; this function never commits.
    """
    context = _prepare_lifecycle(db, context)
    if type(is_covered) is not bool:
        raise CoverageMutationError("Coverage must be a boolean.")
    try:
        with db.no_autoflush:
            lock_coverage_mutations(db)
            mappings = _lifecycle_mappings(
                db, diagnosis_id, service_id, insurer_id, require_current=True
            )
            _validate_lifecycle_actor(db, context)
            if _current_peer(db, diagnosis_id, service_id, insurer_id) is not None:
                raise _identity_conflict("create")
            rule = DiagnosisServiceRule(
                diagnosis_id=diagnosis_id,
                service_id=service_id,
                insurer_id=insurer_id,
                is_covered=is_covered,
                is_deleted=False,
            )
            history = _lifecycle_history(rule, mappings, "CREATE", None, context)
            with _authorize(db, rule, "CREATE", history):
                db.add(rule)
                db.flush()  # Obtain the generated foreign key for the history event.
                history.rule_id = rule.id
                db.add(history)
                db.flush()
        return rule
    except SQLAlchemyError as exc:
        raise _lifecycle_failure("create", exc) from None


_IMPORT_TIMESTAMP_UNSET = object()


def _import_timestamp(value, *, postgres: bool) -> datetime | str:
    # sqlite3 reads legacy timestamp columns as strings. Preserve their instant
    # and timezone where supplied, rather than replacing them with import time.
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            # The previous Core importer delegated parsing to PostgreSQL, which
            # also accepts non-ISO legacy formats. Retain that behavior using
            # bound parameters; invalid values fail with sanitized diagnostics.
            if postgres:
                return value
    raise CoverageMutationError("Imported rule timestamp is invalid.")


@_audited_service
def import_rule(
    db: Session,
    rule_id: int,
    diagnosis_id: int,
    service_id: int,
    is_covered: bool,
    *,
    insurer_id: int | None = None,
    is_deleted: bool = False,
    created_at=_IMPORT_TIMESTAMP_UNSET,
    updated_at=_IMPORT_TIMESTAMP_UNSET,
    context: MutationContext,
) -> DiagnosisServiceRule:
    """Create a destination row from faithful legacy state, not a new policy.

    Explicit IDs and deletion state are retained, including references to deleted
    mappings. The CREATE event describes destination import NOW; supplied source
    timestamps belong only to the rule row. No past lifecycle events are invented.
    Caller owns the transaction and must roll it back if this operation fails.
    """
    context = _prepare_lifecycle(db, context)
    if type(rule_id) is not int:
        raise CoverageMutationError("An explicit imported rule ID is required.")
    if type(is_covered) is not bool or type(is_deleted) is not bool:
        raise CoverageMutationError("Imported rule state must contain booleans.")
    timestamps = {
        name: _import_timestamp(
            value, postgres=db.get_bind().dialect.name == "postgresql"
        )
        for name, value in (("created_at", created_at), ("updated_at", updated_at))
        if value is not _IMPORT_TIMESTAMP_UNSET
    }
    try:
        with db.no_autoflush:
            lock_coverage_mutations(db)
            mappings = _lifecycle_mappings(
                db, diagnosis_id, service_id, insurer_id, require_current=False
            )
            _validate_lifecycle_actor(db, context)
            if (
                not is_deleted
                and _current_peer(db, diagnosis_id, service_id, insurer_id) is not None
            ):
                raise _identity_conflict("import")
            rule = DiagnosisServiceRule(
                id=rule_id,
                diagnosis_id=diagnosis_id,
                service_id=service_id,
                insurer_id=insurer_id,
                is_covered=is_covered,
                is_deleted=is_deleted,
                **timestamps,
            )
            history = _lifecycle_history(rule, mappings, "CREATE", None, context)
            with _authorize(db, rule, "IMPORT", history):
                db.add(rule)
                db.flush()
                history.rule_id = rule.id
                db.add(history)
                db.flush()
        return rule
    except SQLAlchemyError as exc:
        raise _lifecycle_failure("import", exc) from None


def _change_rule_lifecycle(
    db: Session, rule_id: int, *, restore: bool, context: MutationContext
) -> DiagnosisServiceRule:
    context = _prepare_lifecycle(db, context)
    action = "RESTORE" if restore else "SOFT_DELETE"
    try:
        with db.no_autoflush:
            lock_coverage_mutations(db)
            rule = _load_including_deleted(db, DiagnosisServiceRule, rule_id)
            if rule is None:
                raise CoverageMutationError("Rule not found.", code="rule_not_found")
            if rule.is_deleted != restore:
                raise CoverageMutationError(
                    (
                        "Rule is already current."
                        if restore
                        else "Rule is already deleted."
                    ),
                    code="rule_already_current" if restore else "rule_already_deleted",
                )
            mappings = _lifecycle_mappings(
                db,
                rule.diagnosis_id,
                rule.service_id,
                rule.insurer_id,
                require_current=restore,
            )
            _validate_lifecycle_actor(db, context)
            if restore:
                scope = (
                    DiagnosisServiceRule.insurer_id.is_(None)
                    if rule.insurer_id is None
                    else DiagnosisServiceRule.insurer_id == rule.insurer_id
                )
                conflict = db.scalar(
                    select(DiagnosisServiceRule.id)
                    .where(
                        DiagnosisServiceRule.id != rule.id,
                        DiagnosisServiceRule.diagnosis_id == rule.diagnosis_id,
                        DiagnosisServiceRule.service_id == rule.service_id,
                        DiagnosisServiceRule.is_deleted.is_(False),
                        scope,
                    )
                    .limit(1)
                    .execution_options(include_deleted=True)
                )
                if conflict is not None:
                    raise CoverageMutationError(
                        "Another current rule has the same identity.",
                        code="rule_restore_conflict",
                    )
            # Snapshot before changing the target; coverage is never reassigned.
            history = _lifecycle_history(
                rule, mappings, action, rule.is_deleted, context
            )
            with _authorize(db, rule, action, history):
                db.add(history)
                rule.is_deleted = not restore
                db.flush()
        return rule
    except SQLAlchemyError as exc:
        raise _lifecycle_failure(action.lower(), exc) from None


@_audited_service
def soft_delete_rule(
    db: Session, rule_id: int, *, context: MutationContext
) -> DiagnosisServiceRule:
    """Remove an existing rule from resolution, retaining its row and coverage.

    Errors must escape the caller's transaction block so rule and history roll back.
    Deleted mappings can still describe removal; physically absent mappings cannot.
    """
    return _change_rule_lifecycle(db, rule_id, restore=False, context=context)


@_audited_service
def restore_rule(
    db: Session, rule_id: int, *, context: MutationContext
) -> DiagnosisServiceRule:
    """Reactivate a deleted rule only with current mappings and no current peer.

    The shared transaction lock serializes the state/conflict reads under supported
    PostgreSQL READ COMMITTED isolation. Caller owns commit/rollback as above.
    """
    return _change_rule_lifecycle(db, rule_id, restore=True, context=context)
