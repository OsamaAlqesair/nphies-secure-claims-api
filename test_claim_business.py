"""Canonical business evaluation and both public adapters, on disposable databases."""

from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from models import (
    AuditLog,
    Claim,
    CoverageRuleHistory,
    DiagnosisCode,
    InsuranceCompany,
    NphiesTerminology,
    ServiceCode,
)
from services import claim_business
from services.claim_business import BusinessPair, ClaimBusinessEvaluator
from services.coverage import CoverageScope
from services.coverage_mutations import MutationContext, create_rule, soft_delete_rule
from services.terminology import DIAGNOSIS_SYSTEM, SERVICE_SYSTEMS
from test_claim_router import context, payload, rule
from test_coverage_writers import writer_db
from test_terminology_identity import postgres_engine
from testing_catalog import synthetic_catalog, retire_catalog


@pytest.fixture
def business_db(writer_db):
    with writer_db.begin() as db:
        db.add_all(
            [
                DiagnosisCode(id=7, code="G43", description="Synthetic diagnosis"),
                ServiceCode(id=8, code="70450", description="Synthetic service"),
                InsuranceCompany(id=9, name="Synthetic insurer"),
                InsuranceCompany(id=10, name="Other insurer"),
                NphiesTerminology(
                    code="G43", code_system_url=DIAGNOSIS_SYSTEM, display="Diagnosis"
                ),
                NphiesTerminology(
                    code="70450", code_system_url=SERVICE_SYSTEMS[0], display="Service"
                ),
            ]
        )
        synthetic_catalog(db)
    return writer_db


def add_rule(factory, covered, insurer=None, deleted=False):
    with factory.begin() as db:
        created = create_rule(
            db,
            7,
            8,
            covered,
            insurer_id=insurer,
            context=MutationContext(reason="Synthetic evaluator rule"),
        )
        if deleted:
            soft_delete_rule(
                db, created.id, context=MutationContext(reason="Synthetic deleted rule")
            )


@pytest.mark.parametrize(
    "specific,global_rule,reason,scope",
    [
        (True, False, None, CoverageScope.INSURER_SPECIFIC),
        (False, True, "medical_necessity", CoverageScope.INSURER_SPECIFIC),
        (None, True, None, CoverageScope.GLOBAL),
        (None, False, "medical_necessity", CoverageScope.GLOBAL),
        (None, None, "no_coverage_rule", CoverageScope.NONE),
    ],
)
def test_direct_precedence_and_fail_closed(
    business_db, specific, global_rule, reason, scope
):
    if global_rule is not None:
        add_rule(business_db, global_rule)
    if specific is not None:
        add_rule(business_db, specific, 9)
    with business_db() as db:
        result = ClaimBusinessEvaluator(db).evaluate(BusinessPair("G43", "70450", 9))
        assert result.reason == reason
        assert result.is_valid is (reason is None)
        assert result.coverage.selected_scope is scope
        assert (result.coverage.diagnosis_id, result.coverage.service_id) == (7, 8)
        assert result.pair.insurer_id == 9
        assert (
            result.diagnosis.code,
            result.diagnosis.system,
            result.diagnosis.display,
        ) == ("G43", DIAGNOSIS_SYSTEM, "Diagnosis")
        assert (result.service.code, result.service.display) == ("70450", "Service")


def test_direct_deleted_override_and_other_insurer_ignored(business_db):
    add_rule(business_db, True)
    add_rule(business_db, False, 9, deleted=True)
    add_rule(business_db, False, 10)
    with business_db() as db:
        result = ClaimBusinessEvaluator(db).evaluate(BusinessPair("G43", "70450", 9))
        assert result.is_valid
        assert result.coverage.selected_scope is CoverageScope.GLOBAL


@pytest.mark.parametrize("target", [DiagnosisCode, ServiceCode])
def test_direct_deleted_mapping_has_distinct_reason(business_db, target):
    add_rule(business_db, True)
    with business_db.begin() as db:
        db.get(target, 7 if target is DiagnosisCode else 8).soft_delete()
    with business_db() as db:
        result = ClaimBusinessEvaluator(db).evaluate(BusinessPair("G43", "70450"))
        assert not result.is_valid
        assert result.reason == (
            "diagnosis_mapping_missing"
            if target is DiagnosisCode
            else "service_mapping_missing"
        )


@pytest.mark.parametrize(
    "failure",
    ["diagnosis", "service", "catalog", "ambiguous", "system", "deleted_term"],
)
def test_direct_terminology_failures_never_resolve_coverage(business_db, failure):
    with business_db.begin() as db:
        if failure in {"diagnosis", "catalog", "deleted_term"}:
            term = db.scalar(
                sa.select(NphiesTerminology).where(NphiesTerminology.code == "G43")
            )
            if failure == "diagnosis":
                term.is_active = False
            else:
                term.soft_delete()
            if failure != "catalog":
                db.add(NphiesTerminology(code="J00", code_system_url=DIAGNOSIS_SYSTEM))
                synthetic_catalog(db)
            else:
                retire_catalog(db)
        if failure == "service":
            db.scalar(
                sa.select(NphiesTerminology).where(NphiesTerminology.code == "70450")
            ).is_active = False
        if failure == "ambiguous":
            db.add(NphiesTerminology(code="70450", code_system_url=SERVICE_SYSTEMS[1]))
    resolver = Mock(
        side_effect=AssertionError("Ineligible terms must not reach coverage")
    )
    with business_db() as db:
        result = ClaimBusinessEvaluator(db, coverage_resolver=resolver).evaluate(
            BusinessPair(
                "G43",
                "70450",
                service_system=SERVICE_SYSTEMS[1] if failure == "system" else None,
            )
        )
        assert (
            result.reason
            == {
                "diagnosis": "unknown_diagnosis",
                "deleted_term": "unknown_diagnosis",
                "service": "unknown_service",
                "catalog": "icd10_am_catalog_missing",
                "ambiguous": "ambiguous_terminology",
                "system": "unknown_service",
            }[failure]
        )
        assert not result.is_valid
        assert result.coverage is None
    resolver.assert_not_called()


@pytest.mark.parametrize("deleted", [False, True])
def test_direct_unavailable_insurer_never_uses_global(business_db, deleted):
    add_rule(business_db, True)
    if deleted:
        with business_db.begin() as db:
            db.get(InsuranceCompany, 9).soft_delete()
    resolver = Mock(
        side_effect=AssertionError("Unknown insurer must not reach fallback")
    )
    with business_db() as db:
        result = ClaimBusinessEvaluator(db, coverage_resolver=resolver).evaluate(
            BusinessPair("G43", "70450", 9 if deleted else 999)
        )
        assert result.reason == "unknown_insurer"
    resolver.assert_not_called()


def test_direct_memoization_is_scoped_and_request_local(business_db, monkeypatch):
    add_rule(business_db, True)
    add_rule(business_db, False, 9)
    terms = Mock(wraps=claim_business.find_term)
    resolver = Mock(wraps=claim_business.resolve_coverage_by_codes)
    monkeypatch.setattr(claim_business, "find_term", terms)
    with business_db() as db:
        evaluator = ClaimBusinessEvaluator(db, coverage_resolver=resolver)
        for _ in range(3):
            assert evaluator.evaluate(BusinessPair("G43", "70450")).is_valid
            assert (
                evaluator.evaluate(BusinessPair("G43", "70450", 9)).reason
                == "medical_necessity"
            )
        assert terms.call_count == 2
        assert resolver.call_count == 2
    with business_db() as db:
        assert (
            ClaimBusinessEvaluator(db, coverage_resolver=resolver)
            .evaluate(BusinessPair("G43", "70450"))
            .is_valid
        )
    assert terms.call_count == 4
    assert resolver.call_count == 3


def test_direct_evaluation_does_not_write_or_commit(business_db):
    add_rule(business_db, True)
    engine = business_db.kw["bind"]
    statements = []
    commits = []

    def statement(connection, cursor, sql, parameters, context, many):
        statements.append(sql)

    def commit(connection):
        commits.append(True)

    sa.event.listen(engine, "before_cursor_execute", statement)
    sa.event.listen(engine, "commit", commit)
    try:
        with business_db() as db:
            before = tuple(
                db.scalar(sa.select(sa.func.count()).select_from(model))
                for model in (AuditLog, Claim, CoverageRuleHistory)
            )
            assert (
                ClaimBusinessEvaluator(db)
                .evaluate(BusinessPair("G43", "70450"))
                .is_valid
            )
            after = tuple(
                db.scalar(sa.select(sa.func.count()).select_from(model))
                for model in (AuditLog, Claim, CoverageRuleHistory)
            )
            assert before == after
            assert not db.new and not db.dirty and not db.deleted
            assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
            assert not commits
    finally:
        sa.event.remove(engine, "before_cursor_execute", statement)
        sa.event.remove(engine, "commit", commit)


def test_direct_evaluation_does_not_flush_caller_changes(business_db):
    add_rule(business_db, True)
    with business_db() as db:
        pending = NphiesTerminology(code="PENDING", code_system_url=SERVICE_SYSTEMS[0])
        db.add(pending)
        assert (
            ClaimBusinessEvaluator(db).evaluate(BusinessPair("G43", "70450")).is_valid
        )
        assert pending.id is None
        assert pending in db.new
        db.rollback()
    with business_db() as db:
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(NphiesTerminology)
                .where(NphiesTerminology.code == "PENDING")
            )
            == 0
        )


def legacy_payload(payload):
    return {
        "diagnosis_code": payload["claim"]["diagnosis"][0]["diagnosisCodeableConcept"][
            "coding"
        ][0]["code"],
        "service_code": payload["claim"]["item"][0]["productOrService"]["coding"][0][
            "code"
        ],
        "insurer_id": 42,
        "billed_amount": "500.00",
        "allowed_amount": "400.00",
        "copay": "50.00",
        "net_payable": "350.00",
    }


@pytest.mark.parametrize(
    "case,status,reason",
    [
        ("specific_approval", 200, "approved"),
        ("specific_denial", 422, "medical_necessity"),
        ("fallback", 200, "approved"),
        ("global_denial", 422, "medical_necessity"),
        ("missing", 422, "no_coverage_rule"),
        ("deleted_override", 200, "approved"),
        ("other_insurer", 200, "approved"),
        ("deleted_mapping", 422, "diagnosis_mapping_missing"),
        ("catalog", 503, "icd10_am_catalog_missing"),
        ("inactive_term", 422, "unknown_diagnosis"),
        ("ambiguous", 422, "ambiguous_terminology"),
    ],
)
def test_both_routes_share_evaluator_and_audit_semantics(
    context, payload, monkeypatch, case, status, reason
):
    client, sessions = context
    if case not in {"missing", "global_denial"}:
        rule(sessions, case != "specific_approval")
    if case in {"specific_approval", "specific_denial", "deleted_override"}:
        rule(
            sessions,
            case == "specific_approval",
            insurer=42,
            deleted=case == "deleted_override",
        )
    if case == "other_insurer":
        rule(sessions, False, insurer=99)
    if case == "global_denial":
        rule(sessions, False)
    with sessions.begin() as db:
        if case == "deleted_mapping":
            db.get(DiagnosisCode, 1).soft_delete()
        if case in {"catalog", "inactive_term"}:
            terms = db.scalars(
                sa.select(NphiesTerminology).where(
                    NphiesTerminology.code_system_url == DIAGNOSIS_SYSTEM
                )
            ).all()
            for term in terms:
                if case == "catalog":
                    term.soft_delete()
                else:
                    term.is_active = False
            if case == "catalog":
                retire_catalog(db)
            else:
                synthetic_catalog(db)
        if case == "ambiguous":
            db.add(
                NphiesTerminology(
                    code="83600-00-10", code_system_url=SERVICE_SYSTEMS[1]
                )
            )
    calls = []
    original = ClaimBusinessEvaluator.evaluate

    def evaluate(self, pair):
        calls.append(pair)
        return original(self, pair)

    monkeypatch.setattr(ClaimBusinessEvaluator, "evaluate", evaluate)
    legacy = client.post("/process-claim", json=legacy_payload(payload))
    canonical = client.post("/api/v1/claims/pre-validate", json=payload)
    assert legacy.status_code == (
        400 if case in {"inactive_term", "ambiguous"} else status
    )
    assert canonical.status_code == status
    assert len(calls) == 2
    assert (calls[0].diagnosis_code, calls[0].service_code, calls[0].insurer_id) == (
        calls[1].diagnosis_code,
        calls[1].service_code,
        calls[1].insurer_id,
    )
    assert calls[0].service_system is None
    assert calls[1].service_system == SERVICE_SYSTEMS[0]
    if status == 200:
        assert legacy.json()["status"] == "approved"
        assert legacy.json()["net_payable"] == "350.00"
        assert canonical.json()["issue"][0]["severity"] == "information"
        assert "not payer authorization" in canonical.json()["issue"][0]["diagnostics"]
    with sessions() as db:
        audits = db.scalars(
            sa.select(AuditLog)
            .where(AuditLog.action == "claim.validation")
            .order_by(AuditLog.id)
        ).all()
        assert len(audits) == 2
        assert [audit.reason for audit in audits] == [reason, reason]
        assert [audit.http_status for audit in audits] == [
            legacy.status_code,
            canonical.status_code,
        ]
        assert [audit.request_id for audit in audits] == [
            legacy.headers["X-Request-ID"],
            canonical.headers["X-Request-ID"],
        ]
        assert all(audit.actor_user_id is not None for audit in audits)
        assert db.scalar(sa.select(sa.func.count()).select_from(Claim)) == 0


@pytest.mark.parametrize("failure", ["query", "audit", "commit"])
def test_legacy_database_failure_rolls_back_and_is_sanitized(
    context, payload, monkeypatch, failure
):
    import main
    from sqlalchemy.orm import Session

    client, sessions = context
    rule(sessions, True)

    def fail(*args, **kwargs):
        raise OperationalError(
            "sensitive statement", {}, Exception("sensitive database detail")
        )

    rollbacks = []
    original = Session.rollback

    def rolled_back(db):
        rollbacks.append(True)
        return original(db)

    monkeypatch.setattr(Session, "rollback", rolled_back)
    if failure == "query":
        monkeypatch.setattr(ClaimBusinessEvaluator, "evaluate", fail)
    elif failure == "audit":
        monkeypatch.setattr(main, "add_event", fail)
    else:
        monkeypatch.setattr(Session, "commit", fail)
    response = client.post("/process-claim", json=legacy_payload(payload))
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/fhir+json")
    issue = response.json()["issue"][0]
    assert issue["code"] == "transient"
    assert (
        issue["diagnostics"]
        == "Claim validation is temporarily unavailable. Retry later."
    )
    assert "sensitive" not in response.text
    assert rollbacks == [True]
    with sessions() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(AuditLog)) == 0
