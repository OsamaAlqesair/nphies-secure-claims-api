"""Canonical, request-scoped terminology and coverage evaluation.

Adapters own schemas, insurer reference resolution, HTTP responses and audits.
This service only reads through the caller's Session; it never commits or writes.
"""

from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import InsuranceCompany
from services.coverage import (
    COVERAGE_FAILURES,
    CoverageDecision,
    resolve_coverage_by_codes,
)
from services.diagnosis_catalog import (
    DiagnosisCatalogMissingError,
    MISSING_CATALOG_CODE,
    CatalogIdentity,
    require_diagnosis_catalog,
)
from services.terminology import (
    AmbiguousTerminologyError,
    DIAGNOSIS_SYSTEM,
    SERVICE_SYSTEMS,
    find_term,
)


@dataclass(frozen=True)
class BusinessPair:
    diagnosis_code: str
    service_code: str
    insurer_id: int | None = None
    # FHIR supplies an exact system; the legacy code-only contract does not.
    service_system: str | None = None


@dataclass(frozen=True)
class TermIdentity:
    code: str
    system: str
    display: str | None


@dataclass(frozen=True)
class BusinessResult:
    pair: BusinessPair
    diagnosis: TermIdentity | None = None
    service: TermIdentity | None = None
    coverage: CoverageDecision | None = None
    reason: str | None = None
    catalog: CatalogIdentity | None = None

    @property
    def is_valid(self) -> bool:
        return self.coverage is not None and self.coverage.is_covered


class ClaimBusinessEvaluator:
    """One evaluator per request, with memoization across its normalized pairs.

    Legacy insurer IDs are checked after terminology to retain failure precedence.
    FHIR adapters already resolve the available insurer through its Organization.
    The optional resolver lets adapters retain their Phase 10A instrumentation.
    """

    def __init__(
        self,
        db: Session,
        *,
        verify_insurer: bool = True,
        coverage_resolver: Callable[..., CoverageDecision] | None = None,
    ):
        self.db = db
        self.verify_insurer = verify_insurer
        self.coverage_resolver = coverage_resolver or resolve_coverage_by_codes
        self.terms = {}
        self.decisions = {}
        self.insurers = {}
        self.catalog = None
        self.catalog_checked = False

    def _term(self, code, systems):
        key = (code, systems)
        if key not in self.terms:
            if systems == (DIAGNOSIS_SYSTEM,):
                if not self.catalog_checked:
                    self.catalog_checked = True
                    self.catalog = require_diagnosis_catalog(self.db)
                if self.catalog is None:
                    raise DiagnosisCatalogMissingError
                self.terms[key] = find_term(
                    self.db, code, systems, catalog=self.catalog
                )
            else:
                self.terms[key] = find_term(self.db, code, systems)
        return self.terms[key]

    def evaluate(self, pair: BusinessPair) -> BusinessResult:
        # Even a caller's pending ORM changes must remain outside this read service.
        with self.db.no_autoflush:
            return self._evaluate(pair)

    def _evaluate(self, pair: BusinessPair) -> BusinessResult:
        diagnosis = service = None

        def result(reason=None, coverage=None):
            def identity(term):
                return (
                    TermIdentity(term.code, term.code_system_url, term.display)
                    if term is not None
                    else None
                )

            return BusinessResult(
                pair,
                identity(diagnosis),
                identity(service),
                coverage,
                reason,
                self.catalog,
            )

        try:
            diagnosis = self._term(pair.diagnosis_code, (DIAGNOSIS_SYSTEM,))
            service = self._term(pair.service_code, SERVICE_SYSTEMS)
        except DiagnosisCatalogMissingError:
            return result(MISSING_CATALOG_CODE)
        except AmbiguousTerminologyError:
            return result("ambiguous_terminology")
        if diagnosis is None:
            return result("unknown_diagnosis")
        if service is None or (
            pair.service_system is not None
            and service.code_system_url != pair.service_system
        ):
            return result("unknown_service")
        if self.verify_insurer and pair.insurer_id is not None:
            if pair.insurer_id not in self.insurers:
                self.insurers[pair.insurer_id] = (
                    self.db.scalar(
                        select(InsuranceCompany.id).where(
                            InsuranceCompany.id == pair.insurer_id,
                            InsuranceCompany.is_deleted.is_(False),
                        )
                    )
                    is not None
                )
            if not self.insurers[pair.insurer_id]:
                return result("unknown_insurer")
        key = (diagnosis.code, service.code, pair.insurer_id)
        if key not in self.decisions:
            self.decisions[key] = self.coverage_resolver(
                self.db, diagnosis.code, service.code, pair.insurer_id
            )
        decision = self.decisions[key]
        reason = None if decision.is_covered else COVERAGE_FAILURES[decision.status][0]
        return result(reason, decision)
