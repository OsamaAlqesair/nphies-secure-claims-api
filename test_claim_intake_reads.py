"""Ownership-scoped historical GETs on isolated SQLite and PostgreSQL 17."""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

import claim_intake_router
import database
from main import app
from models import (
    ClaimIntake,
    ClaimIntakeEvent,
    ClaimValidationAttempt,
    DiagnosisServiceRule,
    NphiesTerminology,
    User,
)
from schemas.fhir_claim import ClaimSubmission
from services import (
    claim_business,
    claim_intake,
    claim_intake_reads,
    claim_validation,
    coverage,
    terminology,
)
from services.claim_report_serialization import (
    REPORT_VERSION,
    report_outcome,
    serialize_report,
)
from services.coverage_mutations import MutationContext, create_rule, soft_delete_rule
from services.terminology import DIAGNOSIS_SYSTEM
from testing_catalog import synthetic_catalog, retire_catalog


def test_v1_report_roundtrips_without_invented_catalog(
    intake_context, payload, monkeypatch
):
    from services import claim_intake
    from test_claim_intakes import configure, post
    from services.claim_report_serialization import serialize_report

    def legacy_report(report):
        snapshot = serialize_report(report)
        snapshot["schema_version"] = "claim-validation-report-v1"
        for entry in snapshot["observations"]:
            entry["business_result"].pop("catalog")
        return snapshot

    monkeypatch.setattr(claim_intake, "serialize_report", legacy_report)
    monkeypatch.setattr(claim_intake, "REPORT_VERSION", "claim-validation-report-v1")
    configure(intake_context, "PASSED")
    response = post(intake_context, payload)
    assert response.status_code == 201
    public_id = response.json()["public_id"]
    response = get(intake_context, "/" + public_id + "/validations")
    assert response.status_code == 200
    report = response.json()[0]["validation_report"]
    assert report["schema_version"] == "claim-validation-report-v1"
    assert "catalog" not in report["observations"][0]["business_result"]


from test_claim_intakes import PATH, configure, history, intake_context, post
from test_fhir_claim import payload
from test_terminology_identity import postgres_engine

SUFFIXES = ("", "/validations", "/timeline")
SUMMARY_KEYS = {
    "public_id",
    "created_at",
    "state",
    "latest_attempt_no",
    "latest_result",
    "latest_reason",
}


def get(context, suffix="", owner=1, **kwargs):
    return context.client.get(PATH + suffix, headers=context.headers[owner], **kwargs)


@pytest.fixture
def owned_data(intake_context, payload):
    configure(intake_context, "PASSED")
    results = [post(intake_context, payload, owner=owner) for owner in (1, 3, 2, 1)]
    assert all(response.status_code == 201 for response in results)
    return {
        "context": intake_context,
        "ids": [response.json()["public_id"] for response in results],
    }


def append_attempt(context, public_id, number, result, when=None):
    with context.factory.begin() as db:
        if result in {"PASSED", "FAILED"}:
            for rule in db.scalars(sa.select(DiagnosisServiceRule)).all():
                soft_delete_rule(
                    db,
                    rule.id,
                    context=MutationContext(reason="Synthetic history fixture"),
                )
            create_rule(
                db,
                1,
                1,
                result == "PASSED",
                insurer_id=42,
                context=MutationContext(reason="Synthetic history fixture"),
            )
        else:
            retire_catalog(db)
            db.execute(
                sa.update(NphiesTerminology)
                .where(NphiesTerminology.code_system_url == DIAGNOSIS_SYSTEM)
                .values(is_deleted=True)
            )
        intake = db.scalars(
            sa.select(ClaimIntake).where(ClaimIntake.public_id == public_id)
        ).one()
        report = claim_validation.evaluate_report(
            db, ClaimSubmission.model_validate(intake.validated_submission_snapshot)
        )
        assert report.result.value.upper() == result
        db.add(
            ClaimValidationAttempt(
                intake_id=intake.id,
                attempt_no=number,
                actor_user_id=1,
                request_id=str(uuid4()),
                result=result,
                reason="validation_" + result.lower(),
                occurred_at=when or datetime.now(timezone.utc),
                operation_outcome_snapshot=report_outcome(report).model_dump(
                    mode="json", exclude_none=True
                ),
                validation_report_snapshot=serialize_report(report),
                validation_schema_version=REPORT_VERSION,
                revalidation_idempotency_key=str(uuid4()),
            )
        )


@pytest.mark.parametrize("suffix", ["", "/{id}", "/{id}/validations", "/{id}/timeline"])
def test_all_retrieval_routes_require_authentication(owned_data, suffix):
    context = owned_data["context"]
    response = context.client.get(
        PATH + suffix.replace("{id}", owned_data["ids"][0]),
        headers={"Authorization": ""},
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["resourceType"] == "OperationOutcome"
    # Existing global authentication-failure auditing is preserved.
    assert [
        len(rows) for name, rows in history(context).items() if name != "audit_logs"
    ] == [4, 4, 8]


def test_provider_list_scope_and_admin_visibility(owned_data):
    context, ids = owned_data["context"], owned_data["ids"]
    own = get(context).json()
    assert own["limit"] == 50 and own["offset"] == 0 and own["total"] == 2
    assert [row["public_id"] for row in own["items"]] == [ids[3], ids[0]]
    assert all(set(row) == SUMMARY_KEYS for row in own["items"])
    other = get(context, owner=3).json()
    assert other["total"] == 1 and [row["public_id"] for row in other["items"]] == [
        ids[1]
    ]
    admin = get(context, owner=2).json()
    assert (
        admin["total"] == 4
        and [row["public_id"] for row in admin["items"]] == ids[::-1]
    )


@pytest.mark.parametrize("suffix", SUFFIXES)
def test_provider_parent_resolution_is_same_404_and_precedes_child_queries(
    owned_data, suffix
):
    context, ids = owned_data["context"], owned_data["ids"]
    statements = []

    def observe(conn, cursor, statement, parameters, execution_context, executemany):
        statements.append(statement.lower())

    sa.event.listen(context.engine, "before_cursor_execute", observe)
    try:
        inaccessible = get(context, "/" + ids[1] + suffix)
        missing = get(context, "/" + str(uuid4()) + suffix)
    finally:
        sa.event.remove(context.engine, "before_cursor_execute", observe)
    assert inaccessible.status_code == missing.status_code == 404
    assert (
        inaccessible.json()
        == missing.json()
        == {
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "not-found",
                    "diagnostics": "Claim intake not found.",
                }
            ],
        }
    )
    assert inaccessible.headers["content-type"] == missing.headers["content-type"]
    assert (
        inaccessible.headers["Cache-Control"]
        == missing.headers["Cache-Control"]
        == "no-store"
    )
    assert not any(
        "claim_validation_attempts" in query or "claim_intake_events" in query
        for query in statements
    )


@pytest.mark.parametrize("suffix", SUFFIXES)
def test_owners_and_admin_can_retrieve_all_authorized_resources(owned_data, suffix):
    context, ids = owned_data["context"], owned_data["ids"]
    for public_id, owner in zip(ids, (1, 3, 2, 1)):
        assert get(context, "/" + public_id + suffix, owner=owner).status_code == 200
        assert get(context, "/" + public_id + suffix, owner=2).status_code == 200


def test_fhir_provider_identity_never_grants_access(owned_data, payload):
    context, ids = owned_data["context"], owned_data["ids"]
    # Every owner's fixture used exactly the same FHIR provider identity.
    with context.factory() as db:
        providers = {
            row.validated_submission_snapshot["claim"]["provider"]["reference"]
            for row in db.scalars(sa.select(ClaimIntake)).all()
        }
    assert len(providers) == 1
    assert get(context, "/" + ids[1]).status_code == 404
    assert get(context, "/" + ids[0], owner=3).status_code == 404
    assert (
        get(
            context, "/" + ids[1], params={"owner_user_id": 3, "role": "admin"}
        ).status_code
        == 404
    )
    assert get(context, params={"owner_user_id": 3}).json()["total"] == 2


@pytest.mark.parametrize("suffix", SUFFIXES)
@pytest.mark.parametrize(
    "form", ["integer", "invalid", "uppercase", "hex", "braces", "urn"]
)
def test_path_requires_canonical_public_uuid(owned_data, suffix, form):
    key = UUID(owned_data["ids"][0])
    value = {
        "integer": "1",
        "invalid": "private-invalid-path",
        "uppercase": str(key).upper(),
        "hex": key.hex,
        "braces": "{" + str(key) + "}",
        "urn": key.urn,
    }[form]
    response = get(owned_data["context"], "/" + value + suffix)
    assert (
        response.status_code == 422
        and response.json()["resourceType"] == "OperationOutcome"
    )
    assert "private-invalid-path" not in response.text


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": -1},
        {"limit": "bad"},
        {"offset": -1},
        {"offset": "bad"},
        {"limit": "1.5"},
    ],
)
def test_pagination_bounds_are_validated(owned_data, params):
    response = get(owned_data["context"], params=params)
    assert (
        response.status_code == 422
        and response.json()["resourceType"] == "OperationOutcome"
    )


def test_pagination_and_total_do_not_depend_on_page_contents(owned_data):
    context, ids = owned_data["context"], owned_data["ids"]
    page = get(context, params={"limit": 1, "offset": 1}).json()
    assert (page["limit"], page["offset"], page["total"]) == (1, 1, 2)
    assert [row["public_id"] for row in page["items"]] == [ids[0]]
    empty = get(context, params={"limit": 100, "offset": 50}).json()
    assert empty == {"items": [], "limit": 100, "offset": 50, "total": 2}
    admin = get(context, owner=2, params={"limit": 2, "offset": 1}).json()
    assert admin["total"] == 4 and [row["public_id"] for row in admin["items"]] == [
        ids[2],
        ids[1],
    ]


def test_equal_timestamps_use_internal_id_only_for_ordering(intake_context, payload):
    context = intake_context
    same_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with context.factory.begin() as db:
        parent_ids = []
        for number in range(4):
            intake = ClaimIntake(
                owner_user_id=1,
                created_at=same_time,
                idempotency_key=str(uuid4()),
                canonical_input_hash=sha256(str(number).encode()).hexdigest(),
                original_request_snapshot="{}",
                validated_submission_snapshot=ClaimSubmission.model_validate(
                    payload
                ).model_dump(mode="json"),
                schema_version="claim-submission-v1",
            )
            db.add(intake)
            db.flush()
            parent_ids.append(intake.public_id)
            db.add(
                ClaimValidationAttempt(
                    intake_id=intake.id,
                    attempt_no=1,
                    actor_user_id=1,
                    request_id=str(uuid4()),
                    result="PASSED",
                    reason="validation_passed",
                    operation_outcome_snapshot={},
                    validation_report_snapshot={},
                    validation_schema_version=REPORT_VERSION,
                )
            )
    response = get(context)
    assert response.status_code == 200
    assert [row["public_id"] for row in response.json()["items"]] == parent_ids[::-1]
    assert all(set(row) == SUMMARY_KEYS for row in response.json()["items"])


@pytest.mark.parametrize(
    "latest_result,state",
    [
        ("PASSED", "validated"),
        ("FAILED", "validation_failed"),
        ("UNAVAILABLE", "validation_unavailable"),
    ],
)
def test_state_uses_highest_attempt_number_instead_of_time_events_or_claim_status(
    owned_data, latest_result, state
):
    context, public_id = owned_data["context"], owned_data["ids"][0]
    # Insert number 3 before number 2; chronological timestamps disagree too.
    append_attempt(
        context, public_id, 3, latest_result, datetime(2020, 1, 1, tzinfo=timezone.utc)
    )
    if latest_result == "UNAVAILABLE":
        # Restore only fixture terminology before producing the older PASSED attempt.
        with context.factory.begin() as db:
            db.execute(sa.update(NphiesTerminology).values(is_deleted=False))
            synthetic_catalog(db)
    append_attempt(
        context,
        public_id,
        2,
        "FAILED" if latest_result == "PASSED" else "PASSED",
        datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    summary = next(
        row for row in get(context).json()["items"] if row["public_id"] == public_id
    )
    detail = get(context, "/" + public_id).json()
    assert {key: detail[key] for key in SUMMARY_KEYS} == summary
    assert (
        summary["state"],
        summary["latest_attempt_no"],
        summary["latest_result"],
        summary["latest_reason"],
    ) == (state, 3, latest_result, "validation_" + latest_result.lower())
    assert detail["submission"]["claim"]["status"] == "active"


def test_detail_reads_exact_normalized_submission_and_no_private_metadata(owned_data):
    context, public_id = owned_data["context"], owned_data["ids"][0]
    response = get(context, "/" + public_id)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == SUMMARY_KEYS | {"submission"}
    with context.factory() as db:
        stored = db.scalars(
            sa.select(ClaimIntake).where(ClaimIntake.public_id == public_id)
        ).one()
        assert body["submission"] == stored.validated_submission_snapshot
        assert stored.canonical_input_hash not in response.text
        assert stored.idempotency_key not in response.text
        assert stored.original_request_snapshot not in response.text
    assert ClaimSubmission.model_validate(body["submission"]).claim.id == "claim-1"


def test_validation_history_roundtrips_all_results_in_attempt_order(owned_data):
    context, public_id = owned_data["context"], owned_data["ids"][0]
    append_attempt(context, public_id, 2, "FAILED")
    append_attempt(context, public_id, 3, "UNAVAILABLE")
    response = get(context, "/" + public_id + "/validations")
    assert response.status_code == 200
    body = response.json()
    assert [row["attempt_no"] for row in body] == [1, 2, 3]
    assert [row["result"] for row in body] == ["PASSED", "FAILED", "UNAVAILABLE"]
    with context.factory() as db:
        intake_id = db.scalar(
            sa.select(ClaimIntake.id).where(ClaimIntake.public_id == public_id)
        )
        stored = db.scalars(
            sa.select(ClaimValidationAttempt)
            .where(ClaimValidationAttempt.intake_id == intake_id)
            .order_by(ClaimValidationAttempt.attempt_no)
        ).all()
        for returned, original in zip(body, stored):
            assert set(returned) == {
                "attempt_no",
                "result",
                "reason",
                "occurred_at",
                "operation_outcome",
                "validation_report",
            }
            assert returned["operation_outcome"] == original.operation_outcome_snapshot
            assert returned["validation_report"] == original.validation_report_snapshot
            assert (
                original.revalidation_idempotency_key is None
                or original.revalidation_idempotency_key not in response.text
            )


def test_timeline_is_ordered_and_contains_only_controlled_metadata(owned_data):
    context, public_id = owned_data["context"], owned_data["ids"][0]
    with context.factory.begin() as db:
        intake_id = db.scalar(
            sa.select(ClaimIntake.id).where(ClaimIntake.public_id == public_id)
        )
        for number in (4, 3):
            db.add(
                ClaimIntakeEvent(
                    intake_id=intake_id,
                    event_no=number,
                    event_type="validation.completed",
                    actor_user_id=2,
                    request_id=str(uuid4()),
                    reason="validation_completed",
                    details={"attempt_no": number - 1, "result": "FAILED"},
                )
            )
    response = get(context, "/" + public_id + "/timeline")
    assert response.status_code == 200
    body = response.json()
    assert [row["event_no"] for row in body] == [1, 2, 3, 4]
    assert [row["event_type"] for row in body] == [
        "intake.created",
        "validation.completed",
        "validation.completed",
        "validation.completed",
    ]
    assert body[0]["details"] == {}
    assert body[1]["details"] == {"attempt_no": 1, "result": "PASSED"}
    assert all(
        set(row) == {"event_no", "event_type", "occurred_at", "reason", "details"}
        for row in body
    )
    assert all(set(row["details"]) <= {"attempt_no", "result"} for row in body)
    assert "patient-1" not in response.text


def test_reads_do_not_revalidate_query_current_state_write_or_commit(
    owned_data, monkeypatch, caplog
):
    context, public_id = owned_data["context"], owned_data["ids"][0]
    before = history(context)
    with context.factory.begin() as db:
        for rule in db.scalars(sa.select(DiagnosisServiceRule)).all():
            soft_delete_rule(
                db,
                rule.id,
                context=MutationContext(reason="Synthetic current-state change"),
            )
        db.execute(sa.update(NphiesTerminology).values(is_deleted=True))
        retire_catalog(db)
    for module, name in (
        (claim_intake, "evaluate_report"),
        (claim_validation, "evaluate_report"),
        (claim_business.ClaimBusinessEvaluator, "evaluate"),
        (coverage, "resolve_coverage"),
        (terminology, "find_term"),
    ):
        monkeypatch.setattr(
            module,
            name,
            Mock(side_effect=AssertionError("Historical GET evaluated current state")),
        )
    monkeypatch.setattr(
        context.factory.class_,
        "commit",
        Mock(side_effect=AssertionError("GET committed")),
    )
    original_flush = context.factory.class_.flush

    def reject_persistent_flush(db, *args, **kwargs):
        assert not (db.new or db.dirty or db.deleted), "GET attempted persistent writes"
        return original_flush(db, *args, **kwargs)

    monkeypatch.setattr(context.factory.class_, "flush", reject_persistent_flush)
    statements = []

    def observe(conn, cursor, statement, parameters, execution_context, executemany):
        statements.append(statement.lower())

    sa.event.listen(context.engine, "before_cursor_execute", observe)
    try:
        responses = [
            get(context),
            *(get(context, "/" + public_id + suffix) for suffix in SUFFIXES),
        ]
    finally:
        sa.event.remove(context.engine, "before_cursor_execute", observe)
    assert all(response.status_code == 200 for response in responses)
    assert responses[1].json()["latest_result"] == "PASSED"
    assert all(statement.lstrip().startswith("select") for statement in statements)
    assert not any(
        "nphies_terminology" in query or "diagnosis_service_rules" in query
        for query in statements
    )
    assert history(context) == before
    assert "patient-1" not in caplog.text and "Medical Necessity" not in caplog.text


def test_list_query_count_is_fixed_and_database_scope_is_applied(owned_data):
    context, ids = owned_data["context"], owned_data["ids"]
    with context.factory() as db:
        submission = db.scalar(
            sa.select(ClaimIntake.validated_submission_snapshot).where(
                ClaimIntake.public_id == ids[0]
            )
        )
    for _ in range(8):
        assert post(context, submission).status_code == 201
    queries = []

    def observe(conn, cursor, statement, parameters, execution_context, executemany):
        queries.append((statement.lower(), parameters))

    sa.event.listen(context.engine, "before_cursor_execute", observe)
    try:
        small = get(context, params={"limit": 1})
        small_queries = list(queries)
        queries.clear()
        large = get(context, params={"limit": 100})
        large_queries = list(queries)
    finally:
        sa.event.remove(context.engine, "before_cursor_execute", observe)
    assert len(small.json()["items"]) == 1 and len(large.json()["items"]) == 10
    assert len(small_queries) == len(large_queries) <= 4
    history_queries = [query for query, _ in large_queries if "claim_intakes" in query]
    assert history_queries and all(
        "where" in query and "owner_user_id" in query.split("where", 1)[1]
        for query in history_queries
    )
    assert large.json()["total"] == 10
    assert ids[1] not in {row["public_id"] for row in large.json()["items"]}
    assert ids[2] not in {row["public_id"] for row in large.json()["items"]}


def test_empty_list_has_normal_pagination(intake_context):
    response = get(intake_context)
    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 50, "offset": 0, "total": 0}


@pytest.mark.parametrize("suffix", ["", "/validations"])
def test_missing_attempt_fails_closed_instead_of_inventing_pending(
    intake_context, payload, suffix
):
    context = intake_context
    with context.factory.begin() as db:
        intake = ClaimIntake(
            owner_user_id=1,
            idempotency_key=str(uuid4()),
            canonical_input_hash=sha256(b"synthetic").hexdigest(),
            original_request_snapshot="{}",
            validated_submission_snapshot=ClaimSubmission.model_validate(
                payload
            ).model_dump(mode="json"),
            schema_version="claim-submission-v1",
        )
        db.add(intake)
        db.flush()
        public_id = intake.public_id
    response = get(context, "/" + public_id + suffix)
    assert (
        response.status_code == 503
        and response.json()["issue"][0]["code"] == "transient"
    )
    assert get(context).status_code == 503
    assert "pending" not in response.text


@pytest.mark.parametrize(
    "corruption",
    ["submission", "schema_version", "report", "outcome", "validation_version"],
)
def test_corrupt_persisted_snapshots_are_sanitized(intake_context, payload, corruption):
    context = intake_context
    configure(context, "PASSED")
    response = post(context, payload)
    original = history(context)
    values = dict(original["claim_intakes"][0])
    values.pop("id")
    values["public_id"] = str(uuid4())
    values["idempotency_key"] = str(uuid4())
    if corruption == "submission":
        values["validated_submission_snapshot"] = {
            "claim": {"private": "private parser input"}
        }
    elif corruption == "schema_version":
        values["schema_version"] = "unknown-version"
    with context.factory.begin() as db:
        parent = ClaimIntake(**values)
        db.add(parent)
        db.flush()
        attempt = dict(original["claim_validation_attempts"][0])
        attempt.pop("id")
        attempt["intake_id"] = parent.id
        if corruption == "report":
            attempt["validation_report_snapshot"] = {"private": "private parser input"}
        elif corruption == "outcome":
            attempt["operation_outcome_snapshot"] = {
                "resourceType": "OperationOutcome",
                "issue": [{"code": "private", "diagnostics": "private parser input"}],
            }
        elif corruption == "validation_version":
            attempt["validation_schema_version"] = "unknown-version"
        db.add(ClaimValidationAttempt(**attempt))
        public_id = parent.public_id
    suffix = "" if corruption in {"submission", "schema_version"} else "/validations"
    failed = get(context, "/" + public_id + suffix)
    assert (
        failed.status_code == 503 and failed.json()["issue"][0]["code"] == "transient"
    )
    assert "private" not in failed.text and "unknown-version" not in failed.text


@pytest.mark.parametrize("path", ["", "/{id}", "/{id}/validations", "/{id}/timeline"])
def test_database_failures_remain_sanitized(owned_data, path):
    context, public_id = owned_data["context"], owned_data["ids"][0]
    fired = []

    def fail(conn, cursor, statement, parameters, execution_context, executemany):
        if (
            statement.lower().lstrip().startswith("select")
            and "claim_intake" in statement.lower()
        ):
            fired.append(True)
            raise OperationalError("private SQL", {}, Exception("private credentials"))

    sa.event.listen(context.engine, "before_cursor_execute", fail)
    try:
        response = get(context, path.replace("{id}", public_id))
    finally:
        sa.event.remove(context.engine, "before_cursor_execute", fail)
    assert fired and response.status_code == 503
    assert (
        "private" not in response.text
        and response.json()["issue"][0]["code"] == "transient"
    )


def test_all_read_routes_keep_auth_database_failure_handling(owned_data, monkeypatch):
    monkeypatch.setattr(
        claim_intake_router,
        "current_user",
        Mock(
            side_effect=OperationalError(
                "private SQL", {}, Exception("private credentials")
            )
        ),
    )
    context, public_id = owned_data["context"], owned_data["ids"][0]
    for suffix in (
        "",
        "/" + public_id,
        "/" + public_id + "/validations",
        "/" + public_id + "/timeline",
    ):
        response = get(context, suffix)
        assert response.status_code == 503 and "private" not in response.text


def test_read_only_service_does_not_flush_pending_work(owned_data, monkeypatch):
    context, public_id = owned_data["context"], owned_data["ids"][0]
    with context.factory() as db:
        user = db.get(User, 1)
        user.failed_login_attempts += 1
        db.autoflush = True
        monkeypatch.setattr(
            db, "flush", Mock(side_effect=AssertionError("GET flushed pending work"))
        )
        assert claim_intake_reads.intake_detail(
            db, user, UUID(public_id)
        ).public_id == UUID(public_id)
        assert user in db.dirty


def test_route_scope_preserves_creation_and_four_gets():
    document = app.openapi()
    paths = document["paths"]
    assert set(paths[PATH]) == {"post", "get"}
    for suffix in ("/{public_id}", "/{public_id}/timeline"):
        assert set(paths[PATH + suffix]) == {"get"}
    assert set(paths[PATH + "/{public_id}/validations"]) == {"get", "post"}
    for suffix in (
        "",
        "/{public_id}",
        "/{public_id}/validations",
        "/{public_id}/timeline",
    ):
        responses = paths[PATH + suffix]["get"]["responses"]
        for status in ("401", "403", "404", "422", "503"):
            assert set(responses[status]["content"]) == {"application/fhir+json"}
    assert "HTTPValidationError" not in document["components"]["schemas"]
    intake_body = paths[PATH]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]
    canonical_body = paths["/api/v1/claims/pre-validate"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert intake_body == canonical_body
    input_schema = document["components"]["schemas"][
        intake_body["$ref"].rsplit("/", 1)[-1]
    ]
    assert input_schema["title"] == "ClaimSubmission"
    assert set(input_schema["properties"]) == set(ClaimSubmission.model_fields)
