"""Structured observations and compatibility on isolated SQLite/PostgreSQL."""

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, is_dataclass
from enum import Enum
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models import (
    AuditLog,
    Claim,
    DiagnosisCode,
    InsuranceCompany,
    NphiesTerminology,
    Organization,
    ServiceCode,
)
from schemas.fhir_claim import ClaimSubmission
from services import claim_business, claim_validation
from services.claim_business import BusinessResult, ClaimBusinessEvaluator
from services.claim_validation import (
    evaluate,
    evaluate_report,
    report_issues,
    validate_report,
)
from services.claim_validation_report import TerminologyStatus, ValidationResult
from services.coverage import CoverageScope, CoverageStatus
from services.coverage_mutations import MutationContext, create_rule
from services.diagnosis_catalog import MISSING_CATALOG_CODE, MISSING_CATALOG_MESSAGE
from services.terminology import DIAGNOSIS_SYSTEM, SERVICE_SYSTEMS
from test_claim_router import PATH, context, payload, rule
from test_coverage_writers import writer_db
from test_terminology_identity import postgres_engine


@pytest.fixture
def report_db(writer_db):
    with writer_db.begin() as db:
        db.add(
            Organization(
                id=8,
                fhir_id="insurer-1",
                identifier_system="https://example.org/ids",
                identifier_value="test-1",
                name="Synthetic insurer",
                organization_type="insurer",
            )
        )
        db.flush()
        db.add_all(
            [
                InsuranceCompany(id=42, name="Synthetic insurer", organization_id=8),
                DiagnosisCode(id=1, code="E11.9", description="Synthetic diagnosis"),
                DiagnosisCode(id=2, code="K35.80", description="Second diagnosis"),
                ServiceCode(id=1, code="83600-00-10", description="Synthetic service"),
                NphiesTerminology(
                    code_system_url=DIAGNOSIS_SYSTEM, code="E11.9", display="Diagnosis"
                ),
                NphiesTerminology(
                    code_system_url=DIAGNOSIS_SYSTEM, code="K35.80", display="Second"
                ),
                NphiesTerminology(
                    code_system_url=SERVICE_SYSTEMS[0],
                    code="83600-00-10",
                    display="Service",
                ),
            ]
        )
    return writer_db


def add_rule(factory, covered, insurer=None, diagnosis=1):
    with factory.begin() as db:
        return create_rule(
            db,
            diagnosis,
            1,
            covered,
            insurer_id=insurer,
            context=MutationContext(reason="Synthetic structured-report rule"),
        ).id


def multiple_occurrences(payload):
    principal = payload["claim"]["diagnosis"][0]
    principal["sequence"] = 11
    secondary = deepcopy(principal)
    secondary["sequence"] = 23
    secondary["type"][0]["coding"][0]["code"] = "secondary"
    secondary["diagnosisCodeableConcept"]["coding"][0]["code"] = "K35.80"
    payload["claim"]["diagnosis"] = [secondary, principal]
    payload["claim"]["item"][0]["sequence"] = 7
    payload["claim"]["item"][0]["diagnosisSequence"] = [23, 11]
    second = deepcopy(payload["claim"]["item"][0])
    second["sequence"] = 19
    payload["claim"]["item"].append(second)
    payload["claim"]["total"]["value"] = "414.00"
    return ClaimSubmission.model_validate(payload)


@pytest.mark.parametrize(
    "specific,global_rule,scope,covered",
    [
        (True, False, CoverageScope.INSURER_SPECIFIC, True),
        (False, True, CoverageScope.INSURER_SPECIFIC, False),
        (None, True, CoverageScope.GLOBAL, True),
        (None, False, CoverageScope.GLOBAL, False),
        (None, None, CoverageScope.NONE, False),
    ],
)
def test_decision_observations_and_exact_issues(
    report_db, payload, monkeypatch, specific, global_rule, scope, covered
):
    global_id = add_rule(report_db, global_rule) if global_rule is not None else None
    specific_id = add_rule(report_db, specific, 42) if specific is not None else None
    results = []
    original = ClaimBusinessEvaluator.evaluate

    def observe(self, pair):
        result = original(self, pair)
        results.append(result)
        return result

    monkeypatch.setattr(ClaimBusinessEvaluator, "evaluate", observe)
    with report_db() as db:
        report = evaluate_report(db, ClaimSubmission.model_validate(payload))
        assert len(results) == 1
        # Reading/serializing the report must not reconstruct rules or query again.
        monkeypatch.setattr(
            db, "execute", Mock(side_effect=AssertionError("Extra query"))
        )
        monkeypatch.setattr(
            db, "scalar", Mock(side_effect=AssertionError("Extra query"))
        )
        monkeypatch.setattr(
            db, "scalars", Mock(side_effect=AssertionError("Extra query"))
        )
        (observation,) = report.observations
        assert observation.business_result is results[0]
        assert (observation.item_sequence, observation.diagnosis_sequence) == (1, 1)
        assert (observation.diagnosis_code, observation.diagnosis_system) == (
            "E11.9",
            DIAGNOSIS_SYSTEM,
        )
        assert (observation.service_code, observation.service_system) == (
            "83600-00-10",
            SERVICE_SYSTEMS[0],
        )
        assert report.insurer == observation.insurer
        assert report.insurer.reference == "Organization/insurer-1"
        assert report.insurer.fhir_id == "insurer-1"
        assert report.insurer.identifiers == (("https://example.org/ids", "test-1"),)
        assert report.insurer.insurance_company_id == 42
        assert observation.business_result.pair.insurer_id == 42
        assert observation.business_result.diagnosis.display == "Diagnosis"
        assert observation.business_result.service.display == "Service"
        assert observation.terminology_status is TerminologyStatus.VALID
        assert observation.coverage.selected_scope is scope
        assert observation.coverage.insurer_id == (42 if specific is not None else None)
        expected_id = specific_id if specific_id is not None else global_id
        assert observation.coverage.matched_rule_ids == (
            (expected_id,) if expected_id else ()
        )
        assert (observation.coverage.diagnosis_id, observation.coverage.service_id) == (
            1,
            1,
        )
        assert observation.coverage.status is (
            CoverageStatus.APPROVED
            if covered
            else (
                CoverageStatus.NO_APPLICABLE_RULE
                if expected_id is None
                else CoverageStatus.DENIED
            )
        )
        assert report.result is (
            ValidationResult.PASSED if covered else ValidationResult.FAILED
        )
        assert report.termination is None
        assert report.skipped_item_sequences == ()
        issues = report_issues(report)
        if covered:
            assert observation.reason is None
            assert report.findings == ()
            assert issues == []
        else:
            reason = "no_coverage_rule" if expected_id is None else "medical_necessity"
            message = (
                "No applicable coverage rule. Claim denied by default."
                if expected_id is None
                else "Medical Necessity Denied: item 1, service 83600-00-10, diagnosis E11.9."
            )
            assert observation.reason == reason
            assert [issue.model_dump(exclude_none=True) for issue in issues] == [
                {
                    "severity": "error",
                    "code": "business-rule",
                    "diagnostics": message,
                    "details": {
                        "coding": [
                            {
                                "system": "urn:nphies-secure-claim-api:validation",
                                "code": reason,
                            }
                        ]
                    },
                    "expression": [
                        "Claim.item.where(sequence = 1).productOrService",
                        "Claim.diagnosis.where(sequence = 1).diagnosisCodeableConcept",
                    ],
                }
            ]


@pytest.mark.parametrize(
    "target,reason",
    [
        (DiagnosisCode, "diagnosis_mapping_missing"),
        (ServiceCode, "service_mapping_missing"),
    ],
)
def test_missing_mapping_observation(report_db, payload, target, reason):
    add_rule(report_db, True)
    with report_db.begin() as db:
        db.get(target, 1).soft_delete()
    with report_db() as db:
        report = evaluate_report(db, ClaimSubmission.model_validate(payload))
    (observation,) = report.observations
    assert report.result is ValidationResult.FAILED
    assert observation.reason == reason
    assert observation.terminology_status is TerminologyStatus.VALID
    assert observation.coverage.selected_scope is CoverageScope.NONE
    assert observation.coverage.matched_rule_ids == ()
    assert observation.coverage.diagnosis_id == (None if target is DiagnosisCode else 1)
    assert observation.coverage.service_id == (None if target is ServiceCode else 1)


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("diagnosis", "unknown_diagnosis"),
        ("service", "unknown_service"),
        ("ambiguous", "ambiguous_terminology"),
        ("system", "unknown_service"),
        ("catalog", MISSING_CATALOG_CODE),
    ],
)
def test_terminology_and_unavailability(
    report_db, payload, monkeypatch, failure, reason
):
    with report_db.begin() as db:
        if failure in {"diagnosis", "service", "catalog"}:
            query = sa.select(NphiesTerminology).where(
                NphiesTerminology.code_system_url
                == (SERVICE_SYSTEMS[0] if failure == "service" else DIAGNOSIS_SYSTEM)
            )
            for term in db.scalars(query):
                if failure == "catalog":
                    term.soft_delete()
                else:
                    term.is_active = False
        if failure == "ambiguous":
            db.add(
                NphiesTerminology(
                    code="83600-00-10", code_system_url=SERVICE_SYSTEMS[1]
                )
            )
    if failure == "system":
        payload["claim"]["item"][0]["productOrService"]["coding"][0]["system"] = (
            SERVICE_SYSTEMS[1]
        )
    resolver = Mock(
        side_effect=AssertionError("Ineligible terminology reached coverage")
    )
    monkeypatch.setattr(claim_business, "resolve_coverage_by_codes", resolver)
    with report_db() as db:
        report = evaluate_report(db, ClaimSubmission.model_validate(payload))
    (observation,) = report.observations
    assert observation.reason == reason
    assert observation.coverage is None
    assert report.insurer.insurance_company_id == 42
    assert [finding.reason for finding in report.findings] == [reason]
    assert observation.terminology_status is (
        TerminologyStatus.UNAVAILABLE
        if failure == "catalog"
        else TerminologyStatus.INVALID
    )
    if failure == "catalog":
        assert report.result is ValidationResult.UNAVAILABLE
        assert report.termination.stage == "terminology"
        assert (
            report.termination.item_sequence,
            report.termination.diagnosis_sequence,
        ) == (1, 1)
        assert report_issues(report)[0].model_dump(exclude_none=True) == {
            "severity": "error",
            "code": "not-found",
            "diagnostics": MISSING_CATALOG_MESSAGE,
            "expression": ["Claim.diagnosis"],
            "details": {
                "coding": [
                    {"system": "urn:nphies-secure-claim-api:validation", "code": reason}
                ]
            },
        }
    else:
        assert report.result is ValidationResult.FAILED
        assert report.termination is None
    if failure == "system":
        # Preserve the term actually found even when its system fails eligibility.
        assert observation.business_result.service.system == SERVICE_SYSTEMS[0]
        assert observation.service_system == SERVICE_SYSTEMS[1]
    resolver.assert_not_called()


@pytest.mark.parametrize("failure", ["principal", "insurer", "service_coding"])
def test_empty_observations_are_explicit_failures(
    report_db, payload, monkeypatch, failure
):
    if failure == "principal":
        payload["claim"]["diagnosis"][0]["type"][0]["coding"][0]["code"] = "secondary"
    elif failure == "insurer":
        payload["insurer"]["identifier"][0]["value"] = "unlinked"
    else:
        payload["claim"]["item"][0]["productOrService"]["coding"] *= 2
    calls = Mock(side_effect=AssertionError("Business evaluation must not run"))
    monkeypatch.setattr(ClaimBusinessEvaluator, "evaluate", calls)
    with report_db() as db:
        report = evaluate_report(db, ClaimSubmission.model_validate(payload))
    assert report.result is ValidationResult.FAILED
    assert report.observations == ()
    reason = {
        "principal": "invalid_diagnosis",
        "insurer": "unknown_insurer",
        "service_coding": "invalid_service",
    }[failure]
    assert report.findings[0].reason == reason
    if failure == "service_coding":
        assert report.skipped_item_sequences == (1,)
        assert report.termination is None
        assert report.insurer.insurance_company_id == 42
    else:
        assert report.termination.reason == reason
        assert report.insurer.insurance_company_id is None
    calls.assert_not_called()


def test_occurrences_preserve_request_local_memoization(
    report_db, payload, monkeypatch
):
    add_rule(report_db, True)
    add_rule(report_db, False, diagnosis=2)
    submission = multiple_occurrences(payload)
    terms = Mock(wraps=claim_business.find_term)
    resolver = Mock(wraps=claim_business.resolve_coverage_by_codes)
    monkeypatch.setattr(claim_business, "find_term", terms)
    monkeypatch.setattr(claim_business, "resolve_coverage_by_codes", resolver)
    with report_db() as db:
        first = evaluate_report(db, submission)
        assert terms.call_count == 3
        assert resolver.call_count == 2
        second = evaluate_report(db, submission)
        assert terms.call_count == 6
        assert resolver.call_count == 4
    for report in (first, second):
        assert [
            (entry.item_sequence, entry.diagnosis_sequence)
            for entry in report.observations
        ] == [
            (7, 11),
            (7, 23),
            (19, 11),
            (19, 23),
        ]
        assert [entry.reason for entry in report.observations] == [
            None,
            "medical_necessity",
            None,
            "medical_necessity",
        ]
        assert report.observations[0].coverage is report.observations[2].coverage
        assert len(report.findings) == 2
    assert first.observations[0].coverage is not second.observations[0].coverage


def test_catalog_termination_retains_prior_facts_but_preserves_issue_precedence(
    report_db, payload, monkeypatch
):
    submission = multiple_occurrences(payload)
    original = ClaimBusinessEvaluator.evaluate
    calls = []

    def interrupted(self, pair):
        calls.append(pair)
        if len(calls) == 2:
            return BusinessResult(pair, reason=MISSING_CATALOG_CODE)
        return original(self, pair)

    monkeypatch.setattr(ClaimBusinessEvaluator, "evaluate", interrupted)
    with report_db() as db:
        report = evaluate_report(db, submission)
    assert len(calls) == 2
    assert report.result is ValidationResult.UNAVAILABLE
    assert [entry.reason for entry in report.observations] == [
        "no_coverage_rule",
        MISSING_CATALOG_CODE,
    ]
    assert [entry.reason for entry in report.findings] == [
        "no_coverage_rule",
        MISSING_CATALOG_CODE,
    ]
    assert (
        report.termination.item_sequence,
        report.termination.diagnosis_sequence,
    ) == (7, 23)
    assert len(report_issues(report)) == 1
    assert report_issues(report)[0].code == "not-found"


def assert_domain_only(value):
    assert sa.inspect(value, raiseerr=False) is None
    if is_dataclass(value):
        assert value.__dataclass_params__.frozen
        for field in fields(value):
            assert_domain_only(getattr(value, field.name))
    elif isinstance(value, tuple):
        for entry in value:
            assert_domain_only(entry)
    else:
        assert value is None or isinstance(value, (str, int, bool, Enum))


def test_report_is_frozen_domain_data_and_survives_later_rule_changes(
    report_db, payload
):
    add_rule(report_db, True)
    submission = ClaimSubmission.model_validate(payload)
    with report_db() as db:
        report = evaluate_report(db, submission)
    assert_domain_only(report)
    with pytest.raises(FrozenInstanceError):
        report.result = ValidationResult.FAILED
    with pytest.raises(FrozenInstanceError):
        report.observations[0].insurer.insurance_company_id = 99
    add_rule(report_db, False, 42)
    with report_db() as db:
        newer = evaluate_report(db, submission)
    assert report.result is ValidationResult.PASSED
    assert report.observations[0].coverage.selected_scope is CoverageScope.GLOBAL
    assert newer.result is ValidationResult.FAILED
    assert (
        newer.observations[0].coverage.selected_scope is CoverageScope.INSURER_SPECIFIC
    )


def test_issue_wrapper_evaluates_once_and_returns_detached_issues(
    report_db, payload, monkeypatch
):
    submission = ClaimSubmission.model_validate(payload)
    with report_db() as db:
        report = evaluate_report(db, submission)
        generator = Mock(return_value=report)
        monkeypatch.setattr(claim_validation, "evaluate_report", generator)
        issues = evaluate(db, submission)
        generator.assert_called_once_with(db, submission)
    expected = report_issues(report)
    assert issues == expected
    issues[0].expression.append("Changed adapter output")
    issues[0].details["coding"][0]["code"] = "changed"
    assert report_issues(report) == expected
    assert_domain_only(report)


def test_report_never_flushes_writes_or_owns_transaction(
    report_db, payload, monkeypatch
):
    add_rule(report_db, True)
    statements = []
    engine = report_db.kw["bind"]

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())

    with report_db() as db:
        pending = DiagnosisCode(code="PENDING", description="Must remain pending")
        db.add(pending)
        monkeypatch.setattr(db, "commit", Mock(side_effect=AssertionError("Commit")))
        monkeypatch.setattr(
            db, "rollback", Mock(side_effect=AssertionError("Rollback"))
        )
        sa.event.listen(engine, "before_cursor_execute", record)
        try:
            assert (
                evaluate_report(db, ClaimSubmission.model_validate(payload)).result
                is ValidationResult.PASSED
            )
            assert pending in db.new
            assert pending.id is None
            assert statements and set(statements) == {"SELECT"}
        finally:
            sa.event.remove(engine, "before_cursor_execute", record)
    with report_db() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(Claim)) == 0
        assert db.scalar(sa.select(sa.func.count()).select_from(AuditLog)) == 0
        assert (
            db.scalar(sa.select(DiagnosisCode).where(DiagnosisCode.code == "PENDING"))
            is None
        )


def test_database_errors_propagate_instead_of_becoming_business_results(
    report_db, payload, monkeypatch
):
    with report_db() as db:
        monkeypatch.setattr(
            db,
            "scalar",
            Mock(
                side_effect=OperationalError(
                    "Synthetic query", {}, Exception("Synthetic failure")
                )
            ),
        )
        with pytest.raises(OperationalError):
            evaluate_report(db, ClaimSubmission.model_validate(payload))


def test_async_report_and_existing_wrapper_each_traverse_once(
    context, payload, monkeypatch
):
    _, sessions = context
    rule(sessions, True)
    generator = Mock(wraps=claim_validation.evaluate_report)
    monkeypatch.setattr(claim_validation, "evaluate_report", generator)
    submission = ClaimSubmission.model_validate(payload)

    async def run():
        engine = create_async_engine(
            sessions.kw["bind"].url.set(drivername="sqlite+aiosqlite")
        )
        try:
            async with async_sessionmaker(engine)() as db:
                report = await validate_report(db, submission)
                assert report.result is ValidationResult.PASSED
                assert generator.call_count == 1
                assert await claim_validation.validate_coverage(db, submission) == []
                assert generator.call_count == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("covered", [True, False])
def test_public_routes_keep_single_evaluation_and_audits(
    context, payload, monkeypatch, covered
):
    client, sessions = context
    rule(sessions, covered, insurer=42)
    calls = []
    original = ClaimBusinessEvaluator.evaluate

    def observe(self, pair):
        calls.append(pair)
        return original(self, pair)

    monkeypatch.setattr(ClaimBusinessEvaluator, "evaluate", observe)
    canonical = client.post(PATH, json=payload)
    legacy = client.post(
        "/process-claim",
        json={
            "diagnosis_code": "E11.9",
            "service_code": "83600-00-10",
            "insurer_id": 42,
            "billed_amount": "500.00",
            "allowed_amount": "400.00",
            "copay": "50.00",
            "net_payable": "350.00",
        },
    )
    assert len(calls) == 2
    assert canonical.status_code == legacy.status_code == (200 if covered else 422)
    assert set(canonical.json()) == {"resourceType", "issue"}
    if covered:
        assert legacy.json()["status"] == "approved"
        assert legacy.json()["net_payable"] == "350.00"
        assert "not payer authorization" in canonical.json()["issue"][0]["diagnostics"]
    with sessions() as db:
        audits = db.scalars(sa.select(AuditLog).order_by(AuditLog.id)).all()
        assert len(audits) == 2
        assert [entry.reason for entry in audits] == [
            "approved" if covered else "medical_necessity"
        ] * 2
        assert [entry.request_id for entry in audits] == [
            canonical.headers["X-Request-ID"],
            legacy.headers["X-Request-ID"],
        ]
        assert db.scalar(sa.select(sa.func.count()).select_from(Claim)) == 0
