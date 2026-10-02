"""Atomic creation on owned SQLite and disposable PostgreSQL 17; synthetic data."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from decimal import localcontext
import json
from threading import Barrier, Event, Lock
import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from fastapi import Request
from fastapi.testclient import TestClient
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import claim_intake_router
import database
from auth import create_access_token
from main import app
from models import (
    AuditLog,
    Base,
    ClaimIntake,
    ClaimIntakeEvent,
    ClaimValidationAttempt,
    DiagnosisCode,
    InsuranceCompany,
    NphiesTerminology,
    Organization,
    ServiceCode,
    User,
)
from schemas.fhir_claim import ClaimSubmission
from services import claim_intake, claim_validation
from services.claim_business import BusinessResult, ClaimBusinessEvaluator
from services.claim_report_serialization import REPORT_VERSION, serialize_report
from services.coverage_mutations import MutationContext, create_rule
from services.diagnosis_catalog import MISSING_CATALOG_CODE
from services.terminology import DIAGNOSIS_SYSTEM, SERVICE_SYSTEMS
from test_claim_intake_history import isolated_engine
from test_fhir_claim import payload
from test_terminology_identity import postgres_engine
from testing_coverage import owned_sqlite

PATH = "/api/v1/claim-intakes"
TABLES = (ClaimIntake, ClaimValidationAttempt, ClaimIntakeEvent, AuditLog)


@pytest.fixture(params=["sqlite", "postgresql"])
def intake_context(request, monkeypatch):
    if request.param == "postgresql":
        ownership = isolated_engine(request.getfixturevalue("postgres_engine"))
    else:
        engine = sa.create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )

        @sa.event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        ownership = owned_sqlite(engine)
    with ownership as owned:
        if request.param == "postgresql":
            engine = owned
            monkeypatch.setattr(database, "engine", engine)
            command.upgrade(Config("alembic.ini"), "head")
        else:
            Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        with factory.begin() as db:
            users = [
                User(
                    id=key,
                    username=f"synthetic-{key}",
                    role=role,
                    password_hash="synthetic",
                )
                for key, role in ((1, "provider"), (2, "admin"), (3, "provider"))
            ]
            db.add_all(users)
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
                    InsuranceCompany(
                        id=42, name="Synthetic insurer", organization_id=8
                    ),
                    DiagnosisCode(
                        id=1, code="E11.9", description="Synthetic diagnosis"
                    ),
                    ServiceCode(
                        id=1, code="83600-00-10", description="Synthetic service"
                    ),
                    NphiesTerminology(
                        code_system_url=DIAGNOSIS_SYSTEM,
                        code="E11.9",
                        display="Diagnosis",
                    ),
                    NphiesTerminology(
                        code_system_url=SERVICE_SYSTEMS[0],
                        code="83600-00-10",
                        display="Service",
                    ),
                ]
            )
        headers = {
            user.id: {"Authorization": "Bearer " + create_access_token(user)}
            for user in users
        }

        def dependency():
            with factory() as db:
                yield db

        previous = app.dependency_overrides.copy()
        app.dependency_overrides[database.get_db] = dependency
        try:
            with TestClient(app, headers=headers[1]) as client:
                yield SimpleNamespace(
                    client=client, factory=factory, engine=engine, headers=headers
                )
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)
    if request.param == "sqlite":
        engine.dispose()


def configure(context, result):
    with context.factory.begin() as db:
        if result == "UNAVAILABLE":
            db.execute(
                sa.update(NphiesTerminology)
                .where(NphiesTerminology.code_system_url == DIAGNOSIS_SYSTEM)
                .values(is_deleted=True)
            )
            return None
        return create_rule(
            db,
            1,
            1,
            result == "PASSED",
            insurer_id=42,
            context=MutationContext(reason="Synthetic intake fixture"),
        ).id


def post(context, payload, key=None, owner=1, **kwargs):
    headers = {**context.headers[owner], "Idempotency-Key": key or str(uuid4())}
    headers.update(kwargs.pop("headers", {}))
    return context.client.post(PATH, json=payload, headers=headers, **kwargs)


def history(context):
    with context.engine.connect() as connection:
        return {
            model.__tablename__: [
                dict(row)
                for row in connection.execute(
                    sa.select(model.__table__).order_by(model.id)
                ).mappings()
            ]
            for model in TABLES
        }


def assert_counts(context, expected=(1, 1, 2, 1)):
    rows = history(context)
    assert tuple(len(rows[model.__tablename__]) for model in TABLES) == expected
    return rows


@pytest.mark.parametrize("result", ["PASSED", "FAILED", "UNAVAILABLE"])
def test_creation_records_all_results_and_exact_snapshots(
    intake_context, payload, result, monkeypatch, caplog
):
    context = intake_context
    rule_id = configure(context, result)
    raw = " \n" + json.dumps(payload, ensure_ascii=False, indent=3) + "\n "
    generator = Mock(wraps=claim_intake.evaluate_report)
    monkeypatch.setattr(claim_intake, "evaluate_report", generator)
    # Wrap through a function to preserve the evaluator's self binding.
    original = ClaimBusinessEvaluator.evaluate
    pairs = []
    monkeypatch.setattr(
        ClaimBusinessEvaluator,
        "evaluate",
        lambda self, pair: (pairs.append(pair), original(self, pair))[1],
    )
    key = uuid4()
    response = context.client.post(
        PATH,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": key.hex.upper(),
        },
    )
    assert response.status_code == 201, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == {
        "public_id",
        "result",
        "reason",
        "operation_outcome",
        "created_at",
        "attempt_no",
    }
    assert str(UUID(body["public_id"])) == body["public_id"]
    assert body["result"] == result
    assert body["reason"] == "validation_" + result.lower()
    assert body["attempt_no"] == 1
    assert (
        datetime.fromisoformat(body["created_at"].replace("Z", "+00:00")).tzinfo
        is not None
    )
    assert generator.call_count == len(pairs) == 1
    rows = assert_counts(context)
    intake = rows["claim_intakes"][0]
    attempt = rows["claim_validation_attempts"][0]
    events = rows["claim_intake_events"]
    audit = rows["audit_logs"][0]
    assert intake["owner_user_id"] == 1
    assert intake["idempotency_key"] == str(key)
    assert intake["original_request_snapshot"] == raw
    assert intake["schema_version"] == claim_intake.SUBMISSION_VERSION
    assert ClaimSubmission.model_validate(
        intake["validated_submission_snapshot"]
    ) == ClaimSubmission.model_validate(payload)
    assert attempt["result"] == result and attempt["reason"] == body["reason"]
    assert attempt["attempt_no"] == 1 and attempt["actor_user_id"] == 1
    assert attempt["operation_outcome_snapshot"] == body["operation_outcome"]
    assert attempt["validation_schema_version"] == REPORT_VERSION
    report = attempt["validation_report_snapshot"]
    assert (
        report["result"] == result.lower()
        and report["schema_version"] == REPORT_VERSION
    )
    assert report["insurer"] == {
        "reference": "Organization/insurer-1",
        "fhir_id": "insurer-1",
        "identifiers": [["https://example.org/ids", "test-1"]],
        "insurance_company_id": 42,
    }
    observation = report["observations"][0]
    assert (observation["item_sequence"], observation["diagnosis_sequence"]) == (1, 1)
    assert observation["diagnosis_code"] == "E11.9"
    assert observation["service_code"] == "83600-00-10"
    assert observation["diagnosis_system"] == DIAGNOSIS_SYSTEM
    assert observation["service_system"] == SERVICE_SYSTEMS[0]
    assert observation["insurer"] == report["insurer"]
    business = observation["business_result"]
    assert business["pair"]["insurer_id"] == 42
    if result == "UNAVAILABLE":
        assert observation["terminology_status"] == "unavailable"
        assert business["coverage"] is None
        assert report["termination"] == {
            "stage": "terminology",
            "reason": MISSING_CATALOG_CODE,
            "item_sequence": 1,
            "diagnosis_sequence": 1,
        }
    else:
        assert observation["terminology_status"] == "valid"
        assert business["diagnosis"] == {
            "code": "E11.9",
            "system": DIAGNOSIS_SYSTEM,
            "display": "Diagnosis",
        }
        assert business["service"] == {
            "code": "83600-00-10",
            "system": SERVICE_SYSTEMS[0],
            "display": "Service",
        }
        assert business["coverage"] == {
            "status": "approved" if result == "PASSED" else "denied",
            "diagnosis_id": 1,
            "service_id": 1,
            "matched_rule_ids": [rule_id],
            "selected_scope": "insurer-specific",
            "insurer_id": 42,
            "is_covered": result == "PASSED",
        }
    assert [
        (entry["event_no"], entry["event_type"], entry["reason"], entry["details"])
        for entry in events
    ] == [
        (1, "intake.created", "intake_accepted", {}),
        (
            2,
            "validation.completed",
            "validation_completed",
            {"attempt_no": 1, "result": result},
        ),
    ]
    assert all(entry["intake_id"] == intake["id"] for entry in [attempt, *events])
    assert all(entry["actor_user_id"] == 1 for entry in [attempt, *events, audit])
    assert all(
        entry["request_id"] == response.headers["X-Request-ID"]
        for entry in [attempt, *events, audit]
    )
    assert (
        audit["action"],
        audit["reason"],
        audit["outcome"],
        audit["http_status"],
        audit["endpoint"],
    ) == (
        claim_intake.CREATION_ACTION,
        "intake:" + body["public_id"],
        "success",
        201,
        PATH,
    )
    assert "1000000001" not in json.dumps(audit, default=str)
    assert "patient-1" not in caplog.text


@pytest.mark.parametrize(
    "kind",
    ["json", "structure", "financial", "key", "missing_key", "owner", "utf16", "bom"],
)
def test_pre_persistence_failures_leave_no_history_or_creation_audit(
    intake_context, payload, kind, monkeypatch
):
    context = intake_context
    generator = Mock(side_effect=AssertionError("Validation before accepted input"))
    monkeypatch.setattr(claim_intake, "evaluate_report", generator)
    headers = {"Idempotency-Key": str(uuid4()), "Content-Type": "application/json"}
    if kind == "structure":
        payload.pop("patient")
    elif kind == "financial":
        payload["claim"]["total"]["value"] = "208.00"
    elif kind == "key":
        headers["Idempotency-Key"] = "private-invalid-key"
    elif kind == "missing_key":
        headers.pop("Idempotency-Key")
    elif kind == "owner":
        payload["owner_user_id"] = 2
    content = json.dumps(payload)
    if kind == "json":
        content = '{"claim":'
    elif kind == "utf16":
        content = content.encode("utf-16")
    elif kind == "bom":
        content = b"\xef\xbb\xbf" + content.encode()
    response = context.client.post(PATH, content=content, headers=headers)
    assert response.status_code == 422, response.text
    assert response.json()["resourceType"] == "OperationOutcome"
    assert "private-invalid-key" not in response.text
    generator.assert_not_called()
    assert_counts(context, (0, 0, 0, 0))


def test_authentication_required(intake_context, payload):
    response = intake_context.client.post(
        PATH,
        json=payload,
        headers={
            "Authorization": "",
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    rows = assert_counts(intake_context, (0, 0, 0, 1))
    assert rows["audit_logs"][0]["action"] == "auth.authentication"


@pytest.mark.parametrize("owner", [1, 2])
def test_body_identity_cannot_choose_owner(intake_context, payload, owner):
    configure(intake_context, "PASSED")
    payload["provider"]["id"] = "synthetic-3"
    payload["claim"]["provider"]["reference"] = "Organization/synthetic-3"
    response = post(intake_context, payload, owner=owner)
    assert response.status_code == 201
    assert assert_counts(intake_context)["claim_intakes"][0]["owner_user_id"] == owner


@pytest.mark.parametrize("result", ["PASSED", "FAILED", "UNAVAILABLE"])
def test_replay_uses_only_original_immutable_result(
    intake_context, payload, result, monkeypatch
):
    context = intake_context
    configure(context, result)
    key = str(uuid4())
    first = post(context, payload, key)
    assert first.status_code == 201
    previous = history(context)
    # Current terminology changes cannot change the historical response.
    with context.factory.begin() as db:
        db.execute(sa.update(NphiesTerminology).values(is_deleted=True))
    validation = Mock(side_effect=AssertionError("Replay reevaluated"))
    monkeypatch.setattr(claim_intake, "evaluate_report", validation)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    sa.event.listen(context.engine, "before_cursor_execute", record)
    try:
        replay = post(context, payload, UUID(key).hex.upper())
    finally:
        sa.event.remove(context.engine, "before_cursor_execute", record)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers["Cache-Control"] == "no-store"
    validation.assert_not_called()
    assert history(context) == previous
    assert not any(
        "diagnosis_service_rules" in query or "nphies_terminology" in query
        for query in statements
    )


def test_conflict_does_not_evaluate_or_modify_any_history(
    intake_context, payload, monkeypatch
):
    context = intake_context
    configure(context, "PASSED")
    key = str(uuid4())
    assert post(context, payload, key).status_code == 201
    previous = history(context)
    payload["claim"]["id"] = "different-input"
    validation = Mock(side_effect=AssertionError("Conflict reevaluated"))
    monkeypatch.setattr(claim_intake, "evaluate_report", validation)
    response = post(context, payload, key)
    assert response.status_code == 409
    assert response.json()["resourceType"] == "OperationOutcome"
    assert "constraint" not in response.text.lower()
    validation.assert_not_called()
    assert history(context) == previous


def test_different_owners_can_use_the_same_key(intake_context, payload):
    configure(intake_context, "PASSED")
    key = str(uuid4())
    responses = [post(intake_context, payload, key, owner=owner) for owner in (1, 2, 3)]
    assert [response.status_code for response in responses] == [201, 201, 201]
    assert len({response.json()["public_id"] for response in responses}) == 3
    assert {
        row["owner_user_id"]
        for row in assert_counts(intake_context, (3, 3, 6, 3))["claim_intakes"]
    } == {1, 2, 3}


@pytest.mark.parametrize("form", ["canonical", "uppercase", "hex", "urn", "braces"])
def test_header_uuid_uses_history_normalization(intake_context, payload, form):
    key = uuid4()
    value = {
        "canonical": str(key),
        "uppercase": str(key).upper(),
        "hex": key.hex,
        "urn": key.urn,
        "braces": "{" + str(key) + "}",
    }[form]
    assert post(intake_context, payload, value).status_code == 201
    assert assert_counts(intake_context)["claim_intakes"][0]["idempotency_key"] == str(
        key
    )


def test_canonical_hash_ignores_object_order_defaults_decimal_lexemes_and_timezone(
    payload,
):
    first = ClaimSubmission.model_validate(payload)
    equivalent = deepcopy(payload)
    equivalent["claim"]["created"] = "2026-01-01T07:00:00Z"
    equivalent["claim"]["item"][0]["quantity"]["value"] = "2.00"
    equivalent["claim"]["item"][0]["factor"] = "0.900"
    equivalent["claim"]["item"][0]["unitPrice"]["value"] = 100
    equivalent["claim"]["resourceType"] = "Claim"
    equivalent["insurer"] = [equivalent["insurer"]]
    second = ClaimSubmission.model_validate(
        json.loads(json.dumps(equivalent, sort_keys=True))
    )
    with localcontext() as context:
        context.prec = 2
        assert claim_intake.canonical_input_hash(
            first
        ) == claim_intake.canonical_input_hash(second)
    equivalent["patient"]["name"][0]["family"] = "Changed"
    assert claim_intake.canonical_input_hash(
        first
    ) != claim_intake.canonical_input_hash(ClaimSubmission.model_validate(equivalent))


def test_semantically_equivalent_request_replays_and_keeps_first_raw_snapshot(
    intake_context, payload, monkeypatch
):
    configure(intake_context, "PASSED")
    key = str(uuid4())
    first = post(intake_context, payload, key)
    previous = history(intake_context)
    payload["claim"]["item"][0]["quantity"]["value"] = "2.000"
    payload["claim"]["created"] = "2026-01-01T07:00:00Z"
    payload["insurer"] = [payload["insurer"]]
    monkeypatch.setattr(
        claim_intake, "evaluate_report", Mock(side_effect=AssertionError("Replay"))
    )
    replay = post(intake_context, payload, key)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert history(intake_context) == previous


@pytest.mark.parametrize("failure", ["attempt", "event1", "event2", "audit", "commit"])
def test_persistence_failure_rolls_back_entire_creation(
    intake_context, payload, failure
):
    context = intake_context
    configure(context, "PASSED")
    key = str(uuid4())

    def fail(*args):
        raise OperationalError("private SQL", {}, Exception("private credentials"))

    if failure == "commit":
        target, hook = context.factory.class_, "before_commit"
        listener = fail
    else:
        target, hook = {
            "attempt": (ClaimValidationAttempt, "before_insert"),
            "event1": (ClaimIntakeEvent, "before_insert"),
            "event2": (ClaimIntakeEvent, "before_insert"),
            "audit": (AuditLog, "before_insert"),
        }[failure]

        def listener(mapper, connection, row):
            if failure.startswith("event") and row.event_no != int(failure[-1]):
                return
            fail()

    sa.event.listen(target, hook, listener)
    try:
        response = post(context, payload, key)
    finally:
        sa.event.remove(target, hook, listener)
    assert response.status_code == 503, response.text
    assert response.json()["issue"][0]["code"] == "transient"
    assert "private" not in response.text and "SQL" not in response.text
    assert_counts(context, (0, 0, 0, 0))
    assert post(context, payload, key).status_code == 201
    assert_counts(context)


def test_auth_database_failure_is_sanitized(intake_context, payload, monkeypatch):
    monkeypatch.setattr(
        claim_intake_router,
        "current_user",
        Mock(
            side_effect=OperationalError(
                "private SQL", {}, Exception("private credentials")
            )
        ),
    )
    response = post(intake_context, payload)
    assert (
        response.status_code == 503
        and response.json()["issue"][0]["code"] == "transient"
    )
    assert "private" not in response.text
    assert_counts(intake_context, (0, 0, 0, 0))


def test_validation_database_failure_rolls_back_reservation(
    intake_context, payload, monkeypatch
):
    monkeypatch.setattr(
        claim_intake,
        "evaluate_report",
        Mock(
            side_effect=OperationalError(
                "private SQL", {}, Exception("private credentials")
            )
        ),
    )
    response = post(intake_context, payload)
    assert response.status_code == 503
    assert "private" not in response.text
    assert_counts(intake_context, (0, 0, 0, 0))


def test_raw_json_preserves_unicode_order_and_numeric_lexemes(intake_context, payload):
    configure(intake_context, "PASSED")
    payload["provider"]["name"] = "Synthetic \u0645\u0646\u0634\u0623\u0629"
    raw = (
        "\n "
        + json.dumps(payload, ensure_ascii=False, indent=2).replace(
            '"factor": "0.9"', '"factor": 0.900000'
        )
        + " \r\n"
    )
    response = intake_context.client.post(
        PATH,
        content=raw.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert response.status_code == 201
    assert (
        assert_counts(intake_context)["claim_intakes"][0]["original_request_snapshot"]
        == raw
    )


def test_partial_report_facts_survive_catalog_termination(
    intake_context, payload, monkeypatch
):
    configure(intake_context, "FAILED")
    item = deepcopy(payload["claim"]["item"][0])
    item["sequence"] = 2
    payload["claim"]["item"].append(item)
    payload["claim"]["total"]["value"] = "414.00"
    original = ClaimBusinessEvaluator.evaluate
    count = 0

    def evaluate(self, pair):
        nonlocal count
        count += 1
        return (
            original(self, pair)
            if count == 1
            else BusinessResult(pair, reason=MISSING_CATALOG_CODE)
        )

    monkeypatch.setattr(ClaimBusinessEvaluator, "evaluate", evaluate)
    response = post(intake_context, payload)
    assert response.status_code == 201 and response.json()["result"] == "UNAVAILABLE"
    attempt = assert_counts(intake_context)["claim_validation_attempts"][0]
    report = attempt["validation_report_snapshot"]
    assert [row["reason"] for row in report["findings"]] == [
        "medical_necessity",
        MISSING_CATALOG_CODE,
    ]
    assert [row["item_sequence"] for row in report["observations"]] == [1, 2]
    assert (
        report["observations"][0]["business_result"]["coverage"]["status"] == "denied"
    )
    assert report["observations"][1]["business_result"]["coverage"] is None
    assert report["termination"]["item_sequence"] == 2
    # The historical report retains prior findings; the existing outcome adapter
    # continues to apply catalog-unavailability precedence.
    assert len(attempt["operation_outcome_snapshot"]["issue"]) == 1
    assert attempt["operation_outcome_snapshot"] == response.json()["operation_outcome"]


def test_service_rejects_another_operations_pending_writes(intake_context, payload):
    with intake_context.factory() as db:
        pending = Organization(
            fhir_id="pending",
            identifier_system="urn:synthetic",
            identifier_value="pending",
            name="Pending",
            organization_type="provider",
        )
        db.add(pending)
        request = Request(
            {"type": "http", "method": "POST", "path": PATH, "headers": []}
        )
        with pytest.raises(ValueError, match="pending writes"):
            claim_intake.create_intake(
                db,
                ClaimSubmission.model_validate(payload),
                json.dumps(payload),
                uuid4(),
                1,
                request,
            )
        assert pending in db.new and pending.id is None
    assert_counts(intake_context, (0, 0, 0, 0))


def test_domain_exception_is_not_mislabeled_as_database_failure(
    intake_context, payload, monkeypatch
):
    monkeypatch.setattr(
        claim_intake,
        "evaluate_report",
        Mock(side_effect=ValueError("Synthetic domain failure")),
    )
    with pytest.raises(ValueError, match="Synthetic domain failure"):
        post(intake_context, payload)
    assert_counts(intake_context, (0, 0, 0, 0))


@pytest.mark.parametrize(
    "stage", ["principal", "insurer", "service", "terminology", "mapping", "global"]
)
def test_report_serializer_preserves_termination_and_partial_facts(
    intake_context, payload, stage, monkeypatch
):
    context = intake_context
    if stage == "principal":
        payload["claim"]["diagnosis"][0]["type"][0]["coding"][0]["code"] = "secondary"
    elif stage == "insurer":
        payload["insurer"]["identifier"][0]["value"] = "unknown-license"
    elif stage == "service":
        payload["claim"]["item"][0]["productOrService"]["coding"].append(
            deepcopy(payload["claim"]["item"][0]["productOrService"]["coding"][0])
        )
    elif stage == "terminology":
        with context.factory.begin() as db:
            db.execute(
                sa.update(NphiesTerminology)
                .where(NphiesTerminology.code_system_url == SERVICE_SYSTEMS[0])
                .values(is_deleted=True)
            )
    elif stage == "mapping":
        with context.factory.begin() as db:
            db.get(ServiceCode, 1).soft_delete()
    else:
        with context.factory.begin() as db:
            create_rule(
                db, 1, 1, True, context=MutationContext(reason="Synthetic global")
            )
    with context.factory() as db:
        report = claim_validation.evaluate_report(
            db, ClaimSubmission.model_validate(payload)
        )
        monkeypatch.setattr(
            db, "execute", Mock(side_effect=AssertionError("Serializer queried"))
        )
        snapshot = serialize_report(report)
    assert snapshot["result"] == report.result.value
    assert snapshot["skipped_item_sequences"] == list(report.skipped_item_sequences)
    assert [
        (row["reason"], row["diagnostics"], row["expression"], row["code"])
        for row in snapshot["findings"]
    ] == [
        (
            row.reason,
            row.diagnostics,
            list(row.expression) if row.expression is not None else None,
            row.code,
        )
        for row in report.findings
    ]
    if report.termination is not None:
        assert snapshot["termination"]["stage"] == report.termination.stage
        assert snapshot["termination"]["reason"] == report.termination.reason
    else:
        assert snapshot["termination"] is None
    if stage == "service":
        assert (
            snapshot["skipped_item_sequences"] == [1] and snapshot["observations"] == []
        )
    elif stage == "terminology":
        assert snapshot["observations"][0]["terminology_status"] == "invalid"
        assert (
            snapshot["observations"][0]["business_result"]["diagnosis"]["code"]
            == "E11.9"
        )
    elif stage == "mapping":
        assert (
            snapshot["observations"][0]["business_result"]["coverage"]["service_id"]
            is None
        )
    elif stage == "global":
        assert (
            snapshot["observations"][0]["business_result"]["coverage"]["selected_scope"]
            == "global"
        )
    assert json.loads(json.dumps(snapshot)) == snapshot


@pytest.mark.parametrize("intake_context", ["postgresql"], indirect=True)
@pytest.mark.parametrize("different_input", [False, True])
@pytest.mark.parametrize("result", ["PASSED", "FAILED", "UNAVAILABLE"])
def test_postgresql_real_overlapping_creation_race(
    intake_context, payload, different_input, result, monkeypatch
):
    context = intake_context
    configure(context, result)
    key = str(uuid4())
    competing = deepcopy(payload)
    if different_input:
        competing["claim"]["id"] = "competing-input"
    barrier, evaluating, release = Barrier(2), Event(), Event()
    pids, calls = [], []
    lock = Lock()
    original = claim_intake.evaluate_report

    def evaluate(db, submission):
        with lock:
            calls.append(submission.claim.id)
        evaluating.set()
        assert release.wait(10), "Race test did not release the winner"
        return original(db, submission)

    def reserve(conn, cursor, statement, parameters, execution_context, executemany):
        if statement.lstrip().lower().startswith("insert into claim_intakes"):
            with lock:
                pids.append(conn.connection.driver_connection.info.backend_pid)
            barrier.wait(timeout=10)

    monkeypatch.setattr(claim_intake, "evaluate_report", evaluate)
    sa.event.listen(context.engine, "before_cursor_execute", reserve)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(post, context, body, key) for body in (payload, competing)
            ]
            try:
                assert evaluating.wait(10)
                assert len(set(pids)) == 2
                deadline = time.monotonic() + 8
                blocked = False
                while time.monotonic() < deadline:
                    with context.engine.connect() as connection:
                        blocked = bool(
                            connection.scalar(
                                sa.text(
                                    "SELECT count(*) FROM pg_stat_activity WHERE pid = ANY(:pids) "
                                    "AND cardinality(pg_blocking_pids(pid)) > 0"
                                ),
                                {"pids": pids},
                            )
                        )
                    if blocked:
                        break
                    time.sleep(0.02)
                assert blocked, "No actual PostgreSQL unique-index wait observed"
            finally:
                release.set()
            responses = [future.result(timeout=10) for future in futures]
    finally:
        release.set()
        sa.event.remove(context.engine, "before_cursor_execute", reserve)
    assert sorted(response.status_code for response in responses) == (
        [201, 409] if different_input else [200, 201]
    )
    assert len(calls) == 1
    rows = assert_counts(context)
    assert rows["claim_validation_attempts"][0]["result"] == result
    assert (
        rows["claim_intakes"][0]["validated_submission_snapshot"]["claim"]["id"]
        == calls[0]
    )
    if not different_input:
        assert responses[0].json() == responses[1].json()
    else:
        assert (
            next(
                response for response in responses if response.status_code == 409
            ).json()["resourceType"]
            == "OperationOutcome"
        )


@pytest.mark.parametrize("intake_context", ["postgresql"], indirect=True)
def test_postgresql_loser_reserves_key_after_winner_rolls_back(
    intake_context, payload, monkeypatch
):
    context = intake_context
    configure(context, "PASSED")
    key = str(uuid4())
    barrier, evaluated, release = Barrier(2), Event(), Event()
    lock = Lock()
    calls = []
    original = claim_intake.evaluate_report

    def evaluate(db, submission):
        with lock:
            calls.append(submission.claim.id)
            first = len(calls) == 1
        if first:
            evaluated.set()
            assert release.wait(10)
        return original(db, submission)

    def reserve(conn, cursor, statement, parameters, execution_context, executemany):
        if statement.lstrip().lower().startswith("insert into claim_intakes"):
            barrier.wait(timeout=10)

    commits = []

    def fail_first_commit(db):
        with lock:
            commits.append(True)
            first = len(commits) == 1
        if first:
            raise OperationalError(
                "Synthetic commit", {}, Exception("Synthetic failure")
            )

    monkeypatch.setattr(claim_intake, "evaluate_report", evaluate)
    sa.event.listen(context.engine, "before_cursor_execute", reserve)
    sa.event.listen(context.factory.class_, "before_commit", fail_first_commit)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(post, context, payload, key) for _ in range(2)]
            try:
                assert evaluated.wait(10)
            finally:
                release.set()
            responses = [future.result(timeout=10) for future in futures]
    finally:
        release.set()
        sa.event.remove(context.engine, "before_cursor_execute", reserve)
        sa.event.remove(context.factory.class_, "before_commit", fail_first_commit)
    assert sorted(response.status_code for response in responses) == [201, 503]
    assert len(calls) == 2
    assert_counts(context)
    # Only the successful transaction's request ID survives in every history row.
    successful = next(response for response in responses if response.status_code == 201)
    rows = history(context)
    assert all(
        row["request_id"] == successful.headers["X-Request-ID"]
        for name in ("claim_validation_attempts", "claim_intake_events", "audit_logs")
        for row in rows[name]
    )


def test_owner_key_uniqueness_is_database_authoritative(intake_context, payload):
    context = intake_context
    assert post(context, payload).status_code == 201
    previous = history(context)
    values = dict(previous["claim_intakes"][0])
    values.pop("id")
    values["public_id"] = str(uuid4())
    with pytest.raises(SQLAlchemyError):
        with context.engine.begin() as connection:
            connection.execute(ClaimIntake.__table__.insert().values(**values))
    assert history(context) == previous


@pytest.mark.parametrize("intake_context", ["postgresql"], indirect=True)
def test_replay_timestamp_is_stable_across_connection_timezones(
    intake_context, payload
):
    context = intake_context
    configure(context, "PASSED")
    key = str(uuid4())
    zone = ["Pacific/Honolulu"]

    def timezone_for_transaction(connection):
        connection.exec_driver_sql("SET LOCAL TIME ZONE '" + zone[0] + "'")

    sa.event.listen(context.engine, "begin", timezone_for_transaction)
    try:
        creation = post(context, payload, key)
        zone[0] = "Asia/Riyadh"
        replay = post(context, payload, key)
    finally:
        sa.event.remove(context.engine, "begin", timezone_for_transaction)
    assert creation.status_code == 201 and replay.status_code == 200
    assert creation.json() == replay.json()
    assert creation.json()["created_at"].endswith("Z")


@pytest.mark.parametrize(
    "model", [ClaimIntake, ClaimValidationAttempt, ClaimIntakeEvent]
)
def test_created_history_stays_immutable(intake_context, payload, model):
    context = intake_context
    configure(context, "PASSED")
    assert post(context, payload).status_code == 201
    previous = history(context)
    with context.factory() as db:
        row = db.scalars(sa.select(model)).first()
        with pytest.raises(ValueError, match="append-only"):
            row.soft_delete()
        if model is ClaimIntake:
            row.created_at = datetime.now(timezone.utc)
        else:
            row.request_id = str(uuid4())
        with pytest.raises(ValueError, match="append-only"):
            db.flush()
        db.rollback()
    for statement in (
        f"UPDATE {model.__tablename__} SET id = id",
        f"DELETE FROM {model.__tablename__}",
    ):
        with pytest.raises(SQLAlchemyError):
            with context.engine.begin() as connection:
                connection.exec_driver_sql(statement)
    if context.engine.dialect.name == "postgresql":
        with pytest.raises(SQLAlchemyError):
            with context.engine.begin() as connection:
                connection.exec_driver_sql(f"TRUNCATE {model.__tablename__} CASCADE")
    assert history(context) == previous


def test_creation_route_contract_is_preserved():
    operations = app.openapi()["paths"][PATH]
    assert "post" in operations
    assert set(operations["post"]["responses"]) == {
        "200",
        "201",
        "401",
        "403",
        "409",
        "422",
        "503",
    }
    submission_ref = operations["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    assert submission_ref.rsplit("/", 1)[-1] in {
        "ClaimSubmission",
        "ClaimSubmission-Input",
    }
