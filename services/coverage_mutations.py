"""Transaction-bound coverage updates. Other mutation writers join in Phase 10A-2."""

from dataclasses import dataclass
from typing import Literal
import unicodedata
import logging

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from models import (
    CoverageRuleHistory,
    DiagnosisCode,
    DiagnosisServiceRule,
    InsuranceCompany,
    ServiceCode,
    User,
)

logger = logging.getLogger(__name__)


class CoverageMutationError(ValueError):
    """Fixed public-safe diagnostic; never include database exception details."""


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
    source: Literal["update_rule.py"] = "update_rule.py"
    # Only trusted application callers may supply an authenticated actor.
    # The current CLI always leaves this NULL.
    actor_user_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", validate_reason(self.reason))
        if self.source != "update_rule.py":
            raise CoverageMutationError("Unsupported mutation source.")
        if self.actor_user_id is not None and (
            type(self.actor_user_id) is not int or self.actor_user_id <= 0
        ):
            raise CoverageMutationError("Invalid trusted actor reference.")


def lock_coverage_mutations(db: Session) -> None:
    """Share the CLI's existing lock order; caller owns the transaction.

    All supported updates take the same table lock before any row lock, avoiding
    lock-order inversion between direct service callers and exact-target CLI reads.
    SQLite is only used for isolated behavioral tests, not concurrency guarantees.
    """
    if not db.in_transaction():
        raise CoverageMutationError("An active caller-owned transaction is required.")
    if db.get_bind().dialect.name == "postgresql":
        try:
            db.execute(
                text(
                    "LOCK TABLE diagnosis_codes, service_codes, insurance_companies, "
                    "diagnosis_service_rules IN SHARE ROW EXCLUSIVE MODE"
                )
            )
        except SQLAlchemyError as exc:
            # Only controlled codes and exception types; never exception text/tracebacks.
            logger.error(
                "coverage_mutation_lock_failed exception_type=%s", type(exc).__name__
            )
            raise CoverageMutationError(
                "Coverage mutation lock could not be acquired."
            ) from None


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
