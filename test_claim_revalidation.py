"""Atomic revalidation on isolated SQLite and real disposable PostgreSQL 17."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from threading import Barrier, Event, Lock
import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

from fastapi import Request
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

import claim_intake_router
import database
from main import app
from models import (
    AuditLog,
    ClaimIntake,
    ClaimIntakeEvent,
    ClaimValidationAttempt,
    DiagnosisServiceRule,
    NphiesTerminology,
    User,
)
from schemas.fhir_claim import ClaimSubmission
from services import claim_revalidation
from services.claim_business import ClaimBusinessEvaluator
from services.claim_intake import ClaimIntakeUnavailable
from services.coverage_mutations import MutationContext, create_rule, soft_delete_rule
from services.coverage_write_guards import register_coverage_engine
from services.terminology import DIAGNOSIS_SYSTEM
from testing_catalog import retire_catalog, synthetic_catalog
from test_claim_intakes import PATH, configure, history, intake_context, post
from test_fhir_claim import payload
from test_terminology_identity import postgres_engine


@pytest.fixture
def existing(intake_context, payload):
    configure(intake_context, "FAILED")
    response = post(intake_context, payload)
    assert response.status_code == 201
    return intake_context, response.json()["public_id"]


def revalidate(context, public_id, key=None, owner=1, **kwargs):
    headers = {**context.headers[owner], "Idempotency-Key": key or str(uuid4())}
    headers.update(kwargs.pop("headers", {}))
    return context.client.post(
        PATH + "/" + public_id + "/validations", headers=headers, **kwargs
    )


def change_environment(context, result):
    with context.factory.begin() as db:
        if result == "UNAVAILABLE":
            retire_catalog(db)
            db.execute(
                sa.update(NphiesTerminology)
                .where(NphiesTerminology.code_system_url == DIAGNOSIS_SYSTEM)
                .values(is_deleted=True)
            )
        else:
            for rule in db.scalars(sa.select(DiagnosisServiceRule)).all():
                soft_delete_rule(
                    db,
                    rule.id,
                    context=MutationContext(reason="Synthetic current-state change"),
                )
            create_rule(
                db,
                1,
                1,
                result == "PASSED",
                insurer_id=42,
                context=MutationContext(reason="Synthetic current-state change"),
            )


def assert_previous_unchanged(previous, current):
    assert current["claim_intakes"] == previous["claim_intakes"]
    for name in ("claim_validation_attempts", "claim_intake_events", "audit_logs"):
        assert current[name][: len(previous[name])] == previous[name]


def test_catalog_switch_preserves_each_attempt_identity(existing):
    context, public_id = existing
    previous = history(context)
    old_catalog = previous["claim_validation_attempts"][0][
        "validation_report_snapshot"
    ]["observations"][0]["business_result"]["catalog"]
    with context.factory.begin() as db:
        new_id = synthetic_catalog(db)
    assert new_id != old_catalog["id"]
    assert revalidate(context, public_id).status_code == 201
    current = history(context)
    assert_previous_unchanged(previous, current)
    new_catalog = current["claim_validation_attempts"][-1][
        "validation_report_snapshot"
    ]["observations"][0]["business_result"]["catalog"]
    assert new_catalog["id"] == new_id
    response = context.client.get(PATH + "/" + public_id + "/validations")
    assert response.status_code == 200
    assert [
        row["validation_report"]["observations"][0]["business_result"]["catalog"]["id"]
        for row in response.json()
    ] == [old_catalog["id"], new_id]


@pytest.mark.parametrize("result", ["PASSED", "FAILED", "UNAVAILABLE"])
def test_new_revalidation_uses_one_report_and_current_environment(
    existing, result, monkeypatch, caplog
):
    context, public_id = existing
    previous = history(context)
    change_environment(context, result)
    evaluator = Mock(wraps=claim_revalidation.evaluate_report)
    outcome = Mock(wraps=claim_revalidation.report_outcome)
    serializer = Mock(wraps=claim_revalidation.serialize_report)
    monkeypatch.setattr(claim_revalidation, "evaluate_report", evaluator)
    monkeypatch.setattr(claim_revalidation, "report_outcome", outcome)
    monkeypatch.setattr(claim_revalidation, "serialize_report", serializer)
    original = ClaimBusinessEvaluator.evaluate
    pairs = []
    monkeypatch.setattr(
        ClaimBusinessEvaluator,
        "evaluate",
        lambda self, pair: (pairs.append(pair), original(self, pair))[1],
    )
    key = str(uuid4())
    response = revalidate(context, public_id, key)
    assert response.status_code == 201, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == {
        "public_id",
        "attempt_no",
        "result",
        "reason",
        "operation_outcome",
        "occurred_at",
    }
    assert body["public_id"] == public_id and body["attempt_no"] == 2
    assert body["result"] == result and body["reason"] == "validation_" + result.lower()
    assert (
        datetime.fromisoformat(body["occurred_at"].replace("Z", "+00:00")).tzinfo
        is not None
    )
    assert evaluator.call_count == outcome.call_count == serializer.call_count == 1
    assert len(pairs) == 1
    assert outcome.call_args.args[0] is serializer.call_args.args[0]
    assert (
        evaluator.call_args.args[1].model_dump(mode="json")
        == previous["claim_intakes"][0]["validated_submission_snapshot"]
    )
    current = history(context)
    assert_previous_unchanged(previous, current)
    assert [len(current[name]) for name in current] == [1, 2, 3, 2]
    attempt = current["claim_validation_attempts"][-1]
    assert attempt["revalidation_idempotency_key"] == key
    assert attempt["operation_outcome_snapshot"] == body["operation_outcome"]
    assert attempt["validation_report_snapshot"]["result"] == result.lower()
    event = current["claim_intake_events"][-1]
    assert event["event_no"] == 3 and event["event_type"] == "validation.completed"
    assert event["details"] == {"attempt_no": 2, "result": result}
    audit = current["audit_logs"][-1]
    assert audit["action"] == claim_revalidation.REVALIDATION_ACTION
    assert audit["outcome"] == "success" and audit["http_status"] == 201
    assert audit["reason"] == "intake:" + public_id and audit["actor_user_id"] == 1
    assert all(
        row["request_id"] == response.headers["X-Request-ID"]
        for row in (attempt, event, audit)
    )
    assert key not in response.text and "patient-1" not in caplog.text
    assert "Medical Necessity" not in caplog.text

    headers = context.headers[1]
    listing = context.client.get(PATH, headers=headers).json()["items"][0]
    detail = context.client.get(PATH + "/" + public_id, headers=headers).json()
    expected_state = {
        "PASSED": "validated",
        "FAILED": "validation_failed",
        "UNAVAILABLE": "validation_unavailable",
    }[result]
    assert listing["state"] == detail["state"] == expected_state
    assert listing["latest_attempt_no"] == detail["latest_attempt_no"] == 2
    assert (
        detail["submission"]
        == previous["claim_intakes"][0]["validated_submission_snapshot"]
    )
    validations = context.client.get(
        PATH + "/" + public_id + "/validations", headers=headers
    ).json()
    assert [entry["attempt_no"] for entry in validations] == [1, 2]
    assert (
        validations[0]["operation_outcome"]
        == previous["claim_validation_attempts"][0]["operation_outcome_snapshot"]
    )
    assert (
        validations[0]["validation_report"]
        == previous["claim_validation_attempts"][0]["validation_report_snapshot"]
    )
    assert validations[1]["validation_report"] == attempt["validation_report_snapshot"]
    timeline = context.client.get(
        PATH + "/" + public_id + "/timeline", headers=headers
    ).json()
    assert [entry["event_no"] for entry in timeline] == [1, 2, 3]
    assert timeline[-1]["details"] == {"attempt_no": 2, "result": result}
    assert history(context) == current


def test_same_key_replay_is_historical_without_writes_or_evaluation(
    existing, monkeypatch
):
    context, public_id = existing
    key = str(uuid4())
    evaluator = Mock(wraps=claim_revalidation.evaluate_report)
    monkeypatch.setattr(claim_revalidation, "evaluate_report", evaluator)
    first = revalidate(context, public_id, key)
    assert first.status_code == 201 and evaluator.call_count == 1
    change_environment(context, "PASSED")
    newer = revalidate(context, public_id)
    assert newer.status_code == 201 and newer.json()["attempt_no"] == 3
    assert newer.json()["result"] == "PASSED"
    previous = history(context)
    statements = []

    def observe(conn, cursor, statement, parameters, execution_context, executemany):
        statements.append(statement.lower())

    sa.event.listen(context.engine, "before_cursor_execute", observe)
    try:
        monkeypatch.setattr(
            claim_revalidation,
            "evaluate_report",
            Mock(side_effect=AssertionError("Replay evaluated")),
        )
        monkeypatch.setattr(
            claim_revalidation,
            "report_outcome",
            Mock(side_effect=AssertionError("Replay regenerated outcome")),
        )
        monkeypatch.setattr(
            claim_revalidation,
            "serialize_report",
            Mock(side_effect=AssertionError("Replay regenerated report")),
        )
        monkeypatch.setattr(
            context.factory.class_,
            "commit",
            Mock(side_effect=AssertionError("Replay committed")),
        )
        replay = revalidate(context, public_id, key, owner=2)
    finally:
        sa.event.remove(context.engine, "before_cursor_execute", observe)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert history(context) == previous
    assert not any(
        "nphies_terminology" in s or "diagnosis_service_rules" in s for s in statements
    )
    assert not any(
        s.lstrip().startswith(("insert", "update", "delete")) for s in statements
    )


def test_new_keys_run_new_validation_and_same_key_is_independent_per_intake(
    existing, payload, monkeypatch
):
    context, public_id = existing
    other = post(context, payload).json()["public_id"]
    evaluator = Mock(wraps=claim_revalidation.evaluate_report)
    monkeypatch.setattr(claim_revalidation, "evaluate_report", evaluator)
    key = str(uuid4())
    first = revalidate(context, public_id, key)
    independent = revalidate(context, other, key)
    change_environment(context, "PASSED")
    fresh = revalidate(context, public_id)
    assert first.status_code == independent.status_code == fresh.status_code == 201
    assert first.json()["attempt_no"] == independent.json()["attempt_no"] == 2
    assert fresh.json()["attempt_no"] == 3 and fresh.json()["result"] == "PASSED"
    assert evaluator.call_count == 3


def test_authorization_is_parent_first_same_404_without_history_queries(
    existing, payload, monkeypatch
):
    context, public_id = existing
    other = post(context, payload, owner=3).json()["public_id"]
    previous = history(context)
    original_evaluator = claim_revalidation.evaluate_report
    evaluator = Mock(side_effect=AssertionError("Unauthorized validation"))
    monkeypatch.setattr(claim_revalidation, "evaluate_report", evaluator)
    statements = []

    def observe(conn, cursor, statement, parameters, execution_context, executemany):
        statements.append(statement.lower())

    sa.event.listen(context.engine, "before_cursor_execute", observe)
    try:
        inaccessible = revalidate(context, other)
        missing = revalidate(context, str(uuid4()))
        spoofed = revalidate(
            context, other, params={"owner_user_id": 3, "role": "admin"}
        )
    finally:
        sa.event.remove(context.engine, "before_cursor_execute", observe)
    assert inaccessible.status_code == missing.status_code == spoofed.status_code == 404
    assert inaccessible.json() == missing.json() == spoofed.json()
    assert inaccessible.json()["issue"][0]["diagnostics"] == "Claim intake not found."
    assert not any(
        "claim_validation_attempts" in s or "claim_intake_events" in s
        for s in statements
    )
    assert evaluator.call_count == 0 and history(context) == previous
    monkeypatch.setattr(claim_revalidation, "evaluate_report", original_evaluator)
    assert revalidate(context, other, owner=3).status_code == 201
    admin = revalidate(context, other, owner=2)
    assert admin.status_code == 201 and admin.json()["attempt_no"] == 3
    assert history(context)["claim_validation_attempts"][-1]["actor_user_id"] == 2
    # Both intakes contain the same FHIR provider; it never grants ownership.
    assert (
        previous["claim_intakes"][0]["validated_submission_snapshot"]["claim"][
            "provider"
        ]
        == previous["claim_intakes"][1]["validated_submission_snapshot"]["claim"][
            "provider"
        ]
    )


def test_authentication_required(existing, monkeypatch):
    context, public_id = existing
    evaluator = Mock(side_effect=AssertionError("Unauthenticated validation"))
    monkeypatch.setattr(claim_revalidation, "evaluate_report", evaluator)
    previous = history(context)
    response = revalidate(context, public_id, headers={"Authorization": ""})
    assert (
        response.status_code == 401 and response.headers["WWW-Authenticate"] == "Bearer"
    )
    assert response.headers["Cache-Control"] == "no-store"
    assert evaluator.call_count == 0
    current = history(context)
    assert all(
        current[name] == previous[name] for name in current if name != "audit_logs"
    )
    assert all(
        row["action"] != claim_revalidation.REVALIDATION_ACTION
        for row in current["audit_logs"]
    )


@pytest.mark.parametrize("body", ["{}", "null", " ", "malformed", "submission"])
def test_request_body_is_rejected_without_evaluation_or_writes(
    existing, payload, body, monkeypatch
):
    context, public_id = existing
    evaluator = Mock(side_effect=AssertionError("Body-driven validation"))
    monkeypatch.setattr(claim_revalidation, "evaluate_report", evaluator)
    previous = history(context)
    kwargs = {"json": payload} if body == "submission" else {"content": body}
    response = revalidate(context, public_id, **kwargs)
    assert (
        response.status_code == 422
        and response.json()["resourceType"] == "OperationOutcome"
    )
    assert evaluator.call_count == 0 and history(context) == previous


@pytest.mark.parametrize(
    "form", ["missing_key", "bad_key", "integer", "uppercase", "hex", "braces", "urn"]
)
def test_invalid_header_and_noncanonical_path_are_controlled(
    existing, form, monkeypatch
):
    context, public_id = existing
    evaluator = Mock(side_effect=AssertionError("Invalid request validation"))
    monkeypatch.setattr(claim_revalidation, "evaluate_report", evaluator)
    previous = history(context)
    if form == "missing_key":
        response = context.client.post(
            PATH + "/" + public_id + "/validations", headers=context.headers[1]
        )
    elif form == "bad_key":
        response = revalidate(context, public_id, "private-invalid-key")
    else:
        key = UUID(public_id)
        value = {
            "integer": "1",
            "uppercase": public_id.upper(),
            "hex": key.hex,
            "braces": "{" + public_id + "}",
            "urn": key.urn,
        }[form]
        response = revalidate(context, value)
    assert response.status_code == 422 and "private-invalid-key" not in response.text
    assert evaluator.call_count == 0 and history(context) == previous


@pytest.mark.parametrize("form", ["canonical", "uppercase", "hex", "braces", "urn"])
def test_header_uuid_normalizes_to_canonical_stored_key(existing, form):
    context, public_id = existing
    key = uuid4()
    value = {
        "canonical": str(key),
        "uppercase": str(key).upper(),
        "hex": key.hex,
        "braces": "{" + str(key) + "}",
        "urn": key.urn,
    }[form]
    first = revalidate(context, public_id, value)
    replay = revalidate(context, public_id, str(key))
    assert first.status_code == 201 and replay.status_code == 200
    assert first.json() == replay.json()
    assert history(context)["claim_validation_attempts"][-1][
        "revalidation_idempotency_key"
    ] == str(key)


@pytest.mark.parametrize("corruption", ["submission", "version"])
def test_corrupt_frozen_snapshot_fails_closed_without_evaluation(
    existing, corruption, monkeypatch
):
    context, _ = existing
    values = dict(history(context)["claim_intakes"][0])
    values.pop("id")
    public_id = values["public_id"] = str(uuid4())
    values["idempotency_key"] = str(uuid4())
    if corruption == "submission":
        values["validated_submission_snapshot"] = {
            "claim": {"private": "private snapshot content"}
        }
    else:
        values["schema_version"] = "unsupported-version"
    with context.factory.begin() as db:
        db.add(ClaimIntake(**values))
    previous = history(context)
    evaluator = Mock(side_effect=AssertionError("Corrupt input evaluated"))
    monkeypatch.setattr(claim_revalidation, "evaluate_report", evaluator)
    failed = revalidate(context, public_id)
    assert (
        failed.status_code == 503 and failed.json()["issue"][0]["code"] == "transient"
    )
    assert "private" not in failed.text and "unsupported-version" not in failed.text
    assert evaluator.call_count == 0 and history(context) == previous


@pytest.mark.parametrize("failure", ["attempt", "event", "audit", "flush", "commit"])
def test_each_persistence_failure_rolls_back_all_new_rows(
    existing, failure, monkeypatch
):
    context, public_id = existing
    previous = history(context)
    key = str(uuid4())
    fired = []

    def fail(*args):
        fired.append(True)
        raise OperationalError("private SQL", {}, Exception("private credentials"))

    listener = None
    if failure == "flush":
        original = context.factory.class_.flush

        def flush(db, *args, **kwargs):
            if any(isinstance(row, ClaimValidationAttempt) for row in db.new):
                fail()
            return original(db, *args, **kwargs)

        monkeypatch.setattr(context.factory.class_, "flush", flush)
    else:
        target, hook = {
            "attempt": (ClaimValidationAttempt, "before_insert"),
            "event": (ClaimIntakeEvent, "before_insert"),
            "audit": (AuditLog, "before_insert"),
            "commit": (context.factory.class_, "before_commit"),
        }[failure]
        listener = fail
        sa.event.listen(target, hook, listener)
    try:
        failed = revalidate(context, public_id, key)
    finally:
        if listener is not None:
            sa.event.remove(target, hook, listener)
    assert fired and failed.status_code == 503 and "private" not in failed.text
    assert history(context) == previous
    if failure == "flush":
        monkeypatch.setattr(context.factory.class_, "flush", original)
    retry = revalidate(context, public_id, key)
    assert retry.status_code == 201 and retry.json()["attempt_no"] == 2
    assert_previous_unchanged(previous, history(context))


def test_database_and_auth_errors_are_sanitized(existing, monkeypatch):
    context, public_id = existing
    previous = history(context)

    def fail(conn, cursor, statement, parameters, execution_context, executemany):
        if "from claim_intakes" in statement.lower():
            raise OperationalError("private SQL", {}, Exception("private driver"))

    sa.event.listen(context.engine, "before_cursor_execute", fail)
    try:
        response = revalidate(context, public_id)
    finally:
        sa.event.remove(context.engine, "before_cursor_execute", fail)
    assert response.status_code == 503 and "private" not in response.text
    monkeypatch.setattr(
        claim_intake_router,
        "current_user",
        Mock(
            side_effect=OperationalError("private SQL", {}, Exception("private auth"))
        ),
    )
    response = revalidate(context, public_id)
    assert response.status_code == 503 and "private" not in response.text
    assert history(context) == previous


def test_service_rejects_pending_writes_or_caller_owned_transaction(existing):
    context, public_id = existing
    request = Request({"type": "http", "method": "POST", "path": PATH})
    previous = history(context)
    with context.factory() as db:
        user = db.get(User, 1)
        user.failed_login_attempts += 1
        with pytest.raises(ClaimIntakeUnavailable):
            claim_revalidation.revalidate_intake(
                db, user, UUID(public_id), uuid4(), request
            )
        assert user in db.dirty
    with context.factory.begin() as db:
        user = db.get(User, 1)
        with pytest.raises(ClaimIntakeUnavailable):
            claim_revalidation.revalidate_intake(
                db, user, UUID(public_id), uuid4(), request
            )
        assert db.in_transaction()
    assert history(context) == previous


def test_parent_serialization_precedes_all_history_queries(existing):
    context, public_id = existing
    statements = []

    def observe(conn, cursor, statement, parameters, execution_context, executemany):
        statements.append(statement.lower().strip())

    sa.event.listen(context.engine, "before_cursor_execute", observe)
    try:
        assert revalidate(context, public_id).status_code == 201
    finally:
        sa.event.remove(context.engine, "before_cursor_execute", observe)
    parent = next(i for i, s in enumerate(statements) if "from claim_intakes" in s)
    child = next(
        i for i, s in enumerate(statements) if "from claim_validation_attempts" in s
    )
    assert parent < child
    assert "owner_user_id" in statements[parent].split("where", 1)[1]
    if context.engine.dialect.name == "postgresql":
        assert statements[parent].endswith("for update")
    else:
        assert statements.index("begin immediate") < parent


def test_sequence_allocation_uses_highest_numbers_not_row_counts(existing):
    context, public_id = existing
    rows = history(context)
    attempt = dict(rows["claim_validation_attempts"][0])
    attempt.pop("id")
    attempt["attempt_no"] = 7
    attempt["revalidation_idempotency_key"] = str(uuid4())
    event = dict(rows["claim_intake_events"][-1])
    event.pop("id")
    event["event_no"] = 9
    event["details"] = {"attempt_no": 7, "result": attempt["result"]}
    with context.factory.begin() as db:
        db.add(ClaimValidationAttempt(**attempt))
        db.add(ClaimIntakeEvent(**event))
    previous = history(context)
    response = revalidate(context, public_id)
    assert response.status_code == 201 and response.json()["attempt_no"] == 8
    current = history(context)
    assert current["claim_intake_events"][-1]["event_no"] == 10
    assert current["claim_intake_events"][-1]["details"]["attempt_no"] == 8
    assert_previous_unchanged(previous, current)


def test_new_revalidation_commits_once_and_replay_does_not_commit(existing):
    context, public_id = existing
    commits = []

    def committed(db):
        commits.append(True)

    sa.event.listen(context.factory.class_, "after_commit", committed)
    try:
        key = str(uuid4())
        assert revalidate(context, public_id, key).status_code == 201
        assert len(commits) == 1
        assert revalidate(context, public_id, key).status_code == 200
        assert len(commits) == 1
    finally:
        sa.event.remove(context.factory.class_, "after_commit", committed)


@pytest.mark.parametrize(
    "model", [ClaimIntake, ClaimValidationAttempt, ClaimIntakeEvent]
)
def test_append_only_protections_still_apply_after_revalidation(existing, model):
    context, public_id = existing
    assert revalidate(context, public_id).status_code == 201
    previous = history(context)
    with pytest.raises(ValueError, match="append-only"):
        with context.factory.begin() as db:
            row = db.scalars(sa.select(model).order_by(model.id.desc())).first()
            db.delete(row)
    with pytest.raises(SQLAlchemyError):
        with context.engine.begin() as conn:
            conn.execute(sa.delete(model.__table__))
    assert history(context) == previous


@pytest.mark.parametrize("intake_context", ["postgresql"], indirect=True)
@pytest.mark.parametrize("same_key", [True, False])
@pytest.mark.parametrize("rollback_winner", [False, True])
def test_postgresql_real_parent_lock_races_and_rollback(
    existing, same_key, rollback_winner, monkeypatch
):
    context, public_id = existing
    previous = history(context)
    first_key = str(uuid4())
    keys = [first_key, first_key if same_key else str(uuid4())]
    barrier, evaluating, release = Barrier(2), Event(), Event()
    lock = Lock()
    pids, calls, commits = [], [], []
    original = claim_revalidation.evaluate_report

    def evaluate(db, submission):
        with lock:
            calls.append(submission.claim.id)
            first = len(calls) == 1
        if first:
            evaluating.set()
            assert release.wait(12), "Race test did not release lock holder"
        return original(db, submission)

    def before_lock(
        conn, cursor, statement, parameters, execution_context, executemany
    ):
        if (
            "from claim_intakes" in statement.lower()
            and statement.rstrip().lower().endswith("for update")
        ):
            with lock:
                pids.append(conn.connection.driver_connection.info.backend_pid)
            barrier.wait(timeout=12)

    def fail_commit(db):
        with lock:
            commits.append(True)
            first = len(commits) == 1
        if rollback_winner and first:
            raise OperationalError(
                "Synthetic commit", {}, Exception("Synthetic failure")
            )

    monkeypatch.setattr(claim_revalidation, "evaluate_report", evaluate)
    sa.event.listen(context.engine, "before_cursor_execute", before_lock)
    sa.event.listen(context.factory.class_, "before_commit", fail_commit)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(revalidate, context, public_id, key) for key in keys]
            try:
                assert evaluating.wait(12)
                assert len(set(pids)) == 2
                deadline = time.monotonic() + 8
                blocked = False
                while time.monotonic() < deadline:
                    with context.engine.connect() as conn:
                        blocked = bool(
                            conn.scalar(
                                sa.text(
                                    "SELECT count(*) FROM pg_stat_activity WHERE pid = ANY(:pids) AND cardinality(pg_blocking_pids(pid)) > 0"
                                ),
                                {"pids": pids},
                            )
                        )
                    if blocked:
                        break
                    time.sleep(0.02)
                assert blocked, "No actual PostgreSQL parent-row lock wait observed"
                assert len(calls) == 1
            finally:
                release.set()
            responses = [future.result(timeout=15) for future in futures]
    finally:
        release.set()
        sa.event.remove(context.engine, "before_cursor_execute", before_lock)
        sa.event.remove(context.factory.class_, "before_commit", fail_commit)
    expected_statuses = (
        [201, 503] if rollback_winner else [200, 201] if same_key else [201, 201]
    )
    assert sorted(r.status_code for r in responses) == expected_statuses
    assert len(calls) == (1 if same_key and not rollback_winner else 2)
    current = history(context)
    assert_previous_unchanged(previous, current)
    count = 1 if same_key or rollback_winner else 2
    assert [row["attempt_no"] for row in current["claim_validation_attempts"]] == list(
        range(1, 2 + count)
    )
    assert [row["event_no"] for row in current["claim_intake_events"]] == list(
        range(1, 3 + count)
    )
    assert len(current["audit_logs"]) == 1 + count
    if same_key and not rollback_winner:
        assert responses[0].json() == responses[1].json()
    if rollback_winner:
        successful = next(r for r in responses if r.status_code == 201)
        assert successful.json()["attempt_no"] == 2
        for name in ("claim_validation_attempts", "claim_intake_events", "audit_logs"):
            assert current[name][-1]["request_id"] == successful.headers["X-Request-ID"]


@pytest.mark.parametrize("intake_context", ["postgresql"], indirect=True)
def test_postgresql_different_intakes_lock_independently(
    existing, payload, monkeypatch
):
    context, public_id = existing
    other = post(context, payload).json()["public_id"]
    barrier = Barrier(2)
    original = claim_revalidation.evaluate_report

    def evaluate(db, submission):
        barrier.wait(timeout=12)
        return original(db, submission)

    monkeypatch.setattr(claim_revalidation, "evaluate_report", evaluate)
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(revalidate, context, parent, key)
            for parent in (public_id, other)
        ]
        responses = [future.result(timeout=15) for future in futures]
    assert [r.status_code for r in responses] == [201, 201]
    assert [r.json()["attempt_no"] for r in responses] == [2, 2]


@pytest.mark.parametrize("intake_context", ["sqlite"], indirect=True)
def test_sqlite_file_backed_writers_serialize_before_history(
    existing, tmp_path, monkeypatch
):
    source, public_id = existing
    engine = sa.create_engine(
        "sqlite:///" + (tmp_path / "revalidation.sqlite").as_posix(),
        connect_args={"check_same_thread": False, "timeout": 12},
    )

    @sa.event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    register_coverage_engine(engine)
    try:
        # This tmp_path fixture needs no exceptional coverage writer scope:
        # requests only read coverage and append claim history.
        with engine.connect() as dst:
            with source.engine.connect() as src:
                src.connection.driver_connection.backup(
                    dst.connection.driver_connection
                )
            factory = sessionmaker(engine, expire_on_commit=False)

            def dependency():
                with factory() as db:
                    yield db

            previous_dependency = app.dependency_overrides[database.get_db]
            app.dependency_overrides[database.get_db] = dependency
            context = SimpleNamespace(
                client=source.client,
                factory=factory,
                engine=engine,
                headers=source.headers,
            )
            barrier, evaluating, release = Barrier(2), Event(), Event()
            lock = Lock()
            entered, acquired, calls = [], [], []
            original = claim_revalidation.evaluate_report

            def before(
                conn, cursor, statement, parameters, execution_context, executemany
            ):
                if statement.strip().lower() == "begin immediate":
                    with lock:
                        entered.append(id(conn.connection.driver_connection))
                    barrier.wait(timeout=12)

            def after(
                conn, cursor, statement, parameters, execution_context, executemany
            ):
                if statement.strip().lower() == "begin immediate":
                    with lock:
                        acquired.append(True)

            def evaluate(db, submission):
                calls.append(True)
                evaluating.set()
                assert release.wait(12)
                return original(db, submission)

            monkeypatch.setattr(claim_revalidation, "evaluate_report", evaluate)
            sa.event.listen(engine, "before_cursor_execute", before)
            sa.event.listen(engine, "after_cursor_execute", after)
            try:
                key = str(uuid4())
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [
                        pool.submit(revalidate, context, public_id, key)
                        for _ in range(2)
                    ]
                    try:
                        assert evaluating.wait(12)
                        assert len(set(entered)) == 2 and len(acquired) == 1
                        assert len(calls) == 1
                    finally:
                        release.set()
                    responses = [future.result(timeout=15) for future in futures]
            finally:
                release.set()
                app.dependency_overrides[database.get_db] = previous_dependency
                sa.event.remove(engine, "before_cursor_execute", before)
                sa.event.remove(engine, "after_cursor_execute", after)
            assert sorted(r.status_code for r in responses) == [200, 201]
            assert len(calls) == 1 and len(acquired) == 2
            assert [len(rows) for rows in history(context).values()] == [1, 2, 3, 2]
    finally:
        engine.dispose()


def test_openapi_adds_only_revalidation_and_no_body():
    paths = app.openapi()["paths"]
    operation = paths[PATH + "/{public_id}/validations"]["post"]
    assert "requestBody" not in operation
    assert set(operation["responses"]) == {
        "200",
        "201",
        "401",
        "403",
        "404",
        "422",
        "503",
    }
    assert operation["security"]
    assert any(
        p["name"] == "Idempotency-Key" and p["in"] == "header" and p["required"]
        for p in operation["parameters"]
    )
    for status in ("401", "403", "404", "422", "503"):
        assert set(operation["responses"][status]["content"]) == {
            "application/fhir+json"
        }
    assert set(paths[PATH]) == {"get", "post"}
    assert set(paths[PATH + "/{public_id}"]) == {"get"}
    assert set(paths[PATH + "/{public_id}/timeline"]) == {"get"}
