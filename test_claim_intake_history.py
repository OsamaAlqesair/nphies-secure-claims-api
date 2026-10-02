"""Immutable intake history on in-memory SQLite and disposable PostgreSQL 17.

All payloads are synthetic. No application database or repository dotenv is
used by these fixtures; transaction boundaries remain with their callers.
"""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

import database
from models import (
    Base,
    Claim,
    ClaimIntake,
    ClaimValidationAttempt,
    ClaimIntakeEvent,
    DiagnosisCode,
    ServiceCode,
    NphiesTerminology,
    User,
)
from schemas.fhir_claim import ClaimSubmission
from services.claim_validation import evaluate_report, report_issues
from services.coverage_mutations import MutationContext, create_rule
from test_claim_router import payload
from test_claim_validation_report import report_db, add_rule
from test_coverage_writers import writer_db
from test_terminology_identity import postgres_engine
from testing_coverage import owned_postgres_schema, owned_sqlite

MODELS = (ClaimIntake, ClaimValidationAttempt, ClaimIntakeEvent)
ROOT = Path(__file__).parent
HEAD = "0010_claim_intake_history"
PARENT = "0009_coverage_rule_current_identity"
REQUEST_ID = "00000000-0000-4000-8000-000000000001"


def config():
    return Config(str(ROOT / "alembic.ini"))


@contextmanager
def isolated_engine(root=None):
    schema = None
    if root is None:
        engine = sa.create_engine("sqlite://")

        @sa.event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    else:
        assert root.url.host == "127.0.0.1"
        assert root.url.database == "nphies_identity_test"
        schema = "history_test_" + uuid4().hex
        with root.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = sa.create_engine(
            root.url, connect_args={"options": f"-csearch_path={schema}"}
        )
    try:
        with (
            owned_sqlite(engine)
            if root is None
            else owned_postgres_schema(engine, root, schema)
        ):
            yield engine
    finally:
        engine.dispose()
        if root is not None:
            with root.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture(params=["sqlite", "postgresql"])
def history_engine(request, monkeypatch):
    root = (
        request.getfixturevalue("postgres_engine")
        if request.param == "postgresql"
        else None
    )
    with isolated_engine(root) as engine:
        monkeypatch.setattr(database, "engine", engine)
        yield engine


@pytest.fixture
def history_db(history_engine):
    if history_engine.dialect.name == "sqlite":
        Base.metadata.create_all(history_engine)
    else:
        command.upgrade(config(), "head")
    return sessionmaker(history_engine, expire_on_commit=False)


def users(db):
    db.add_all(
        [
            User(
                id=1,
                username="intake-owner",
                password_hash="synthetic",
                role="provider",
            ),
            User(
                id=2, username="other-owner", password_hash="synthetic", role="provider"
            ),
        ]
    )
    db.flush()


def intake_values(owner=1, **changes):
    original = '{ "claim": {"synthetic": true, "amount": 1.2300}, "note": "TEST ONLY" }'
    values = dict(
        owner_user_id=owner,
        idempotency_key=str(uuid4()),
        canonical_input_hash=sha256(b"synthetic canonical input").hexdigest(),
        original_request_snapshot=original,
        validated_submission_snapshot={
            "claim": {"synthetic": True, "amount": "1.2300"}
        },
        schema_version="claim-submission-v1",
    )
    values.update(changes)
    return values


def attempt_values(intake_id, number=1):
    return dict(
        intake_id=intake_id,
        attempt_no=number,
        actor_user_id=1,
        request_id=REQUEST_ID,
        result="PASSED",
        reason="validation_passed",
        operation_outcome_snapshot={"resourceType": "OperationOutcome", "issue": []},
        validation_report_snapshot={
            "result": "passed",
            "observations": [],
            "findings": [],
        },
        validation_schema_version="claim-validation-report-v1",
    )


def event_values(intake_id, number=1):
    return dict(
        intake_id=intake_id,
        event_no=number,
        event_type="intake.created",
        actor_user_id=1,
        request_id=REQUEST_ID,
        reason="intake_accepted",
        details={},
    )


def history(db):
    intake = ClaimIntake(**intake_values())
    db.add(intake)
    db.flush()
    attempt = ClaimValidationAttempt(**attempt_values(intake.id))
    event = ClaimIntakeEvent(**event_values(intake.id))
    db.add_all([attempt, event])
    db.flush()
    return intake, attempt, event


@pytest.fixture
def records(history_db):
    with history_db.begin() as db:
        users(db)
        rows = history(db)
    return rows


def persisted(factory):
    with factory.kw["bind"].connect() as connection:
        return {
            model.__tablename__: connection.execute(
                sa.select(model.__table__).order_by(model.id)
            ).all()
            for model in MODELS
        }


def core_values(record):
    return {
        column.name: getattr(record, column.name) for column in record.__table__.columns
    }


def test_schema_and_valid_history(history_db, records):
    with history_db() as db:
        intake, attempt, event = (
            db.get(model, record.id) for model, record in zip(MODELS, records)
        )
        assert isinstance(intake.id, int)
        assert UUID(intake.public_id).version == 4
        assert intake.created_at and attempt.occurred_at and event.occurred_at
        assert intake.owner_user_id == attempt.actor_user_id == event.actor_user_id == 1
        assert intake.original_request_snapshot == records[0].original_request_snapshot
        assert (
            intake.validated_submission_snapshot
            == records[0].validated_submission_snapshot
        )
        assert (
            attempt.operation_outcome_snapshot == records[1].operation_outcome_snapshot
        )
        assert (
            attempt.validation_report_snapshot == records[1].validation_report_snapshot
        )
        assert (attempt.attempt_no, event.event_no) == (1, 1)
        assert sa.inspect(Claim).local_table is Base.metadata.tables["claims"]
        assert db.scalar(sa.select(sa.func.count()).select_from(Claim)) == 0
    for model in MODELS:
        assert not {
            "updated_at",
            "is_deleted",
            "status",
            "attempt_count",
            "event_count",
        } & set(model.__table__.c.keys())


def test_uuid_normalization_and_public_uniqueness(history_db, records):
    with history_db.begin() as db:
        key = uuid4()
        first = ClaimIntake(**intake_values(idempotency_key=key.hex.upper()))
        second = ClaimIntake(**intake_values())
        db.add_all([first, second])
        db.flush()
        assert first.idempotency_key == str(key)
        assert first.public_id != second.public_id != records[0].public_id
    with pytest.raises(sa.exc.IntegrityError):
        with history_db.begin() as db:
            db.add(ClaimIntake(**intake_values(public_id=records[0].public_id)))
    for field in ("public_id", "idempotency_key"):
        with pytest.raises(ValueError, match="UUID-compatible"):
            ClaimIntake(**intake_values(**{field: "not a UUID"}))


def test_owner_scoped_creation_idempotency(history_db, records):
    values = core_values(records[0])
    values.pop("id")
    values["public_id"] = str(uuid4())
    values["canonical_input_hash"] = "f" * 64
    before = persisted(history_db)
    with pytest.raises(sa.exc.IntegrityError):
        with history_db.kw["bind"].begin() as connection:
            connection.execute(ClaimIntake.__table__.insert(), values)
    assert persisted(history_db) == before
    values["owner_user_id"] = 2
    with history_db.kw["bind"].begin() as connection:
        connection.execute(ClaimIntake.__table__.insert(), values)


@pytest.mark.parametrize(
    "model,field",
    [(ClaimValidationAttempt, "attempt_no"), (ClaimIntakeEvent, "event_no")],
)
def test_child_numbers_are_positive_and_unique_per_parent(
    history_db, records, model, field
):
    original = records[MODELS.index(model)]
    values = core_values(original)
    values.pop("id")
    before = persisted(history_db)
    for number in (original.__dict__[field], 0, -1):
        values[field] = number
        with pytest.raises(sa.exc.IntegrityError):
            with history_db.kw["bind"].begin() as connection:
                connection.execute(model.__table__.insert(), values)
        assert persisted(history_db) == before
    with history_db.begin() as db:
        other = ClaimIntake(**intake_values())
        db.add(other)
        db.flush()
        values.update(intake_id=other.id, **{field: 1})
        db.execute(model.__table__.insert(), values)


def test_revalidation_key_is_nullable_and_intake_scoped(history_db, records):
    key = uuid4()
    with history_db.begin() as db:
        first = ClaimValidationAttempt(
            **attempt_values(records[0].id, 2), revalidation_idempotency_key=key
        )
        db.add_all([first, ClaimValidationAttempt(**attempt_values(records[0].id, 3))])
        db.flush()
        assert first.revalidation_idempotency_key == str(key)
        other = ClaimIntake(**intake_values())
        db.add(other)
        db.flush()
        db.add(
            ClaimValidationAttempt(
                **attempt_values(other.id), revalidation_idempotency_key=key
            )
        )
    before = persisted(history_db)
    with pytest.raises(sa.exc.IntegrityError):
        with history_db.begin() as db:
            db.add(
                ClaimValidationAttempt(
                    **attempt_values(records[0].id, 4), revalidation_idempotency_key=key
                )
            )
    assert persisted(history_db) == before


@pytest.mark.parametrize("result", ["PASSED", "FAILED", "UNAVAILABLE"])
def test_supported_results_and_completion_events(history_db, records, result):
    with history_db.begin() as db:
        values = attempt_values(records[0].id, 2)
        values.update(result=result, reason="validation_" + result.lower())
        db.add(ClaimValidationAttempt(**values))
        event = event_values(records[0].id, 2)
        event.update(
            event_type="validation.completed",
            reason="validation_completed",
            details={"attempt_no": 2, "result": result},
        )
        db.add(ClaimIntakeEvent(**event))


@pytest.mark.parametrize(
    "model,field,value",
    [
        (ClaimValidationAttempt, "result", "OTHER"),
        (ClaimValidationAttempt, "reason", "raw exception text"),
        (ClaimValidationAttempt, "reason", "validation_failed"),
        (ClaimIntakeEvent, "event_type", "validation.failed"),
        (ClaimIntakeEvent, "event_type", "validation.unavailable"),
        (ClaimIntakeEvent, "reason", "freeform patient data"),
        (ClaimIntakeEvent, "reason", "validation_completed"),
        (ClaimIntake, "canonical_input_hash", "g" * 64),
        (ClaimIntake, "canonical_input_hash", "a" * 63),
        (ClaimIntake, "schema_version", " "),
        (ClaimValidationAttempt, "validation_schema_version", ""),
        (ClaimValidationAttempt, "request_id", ""),
        (ClaimIntakeEvent, "request_id", " "),
    ],
)
def test_invalid_scalar_values_rejected(history_db, records, model, field, value):
    values = core_values(records[MODELS.index(model)])
    values["id"] = 900
    if model is ClaimIntake:
        values.update(public_id=str(uuid4()), idempotency_key=str(uuid4()))
    else:
        values["attempt_no" if model is ClaimValidationAttempt else "event_no"] = 2
    values[field] = value
    with pytest.raises(sa.exc.IntegrityError):
        with history_db.kw["bind"].begin() as connection:
            connection.execute(model.__table__.insert(), values)


REQUIRED = [
    (model, column.name)
    for model in MODELS
    for column in model.__table__.columns
    if not column.nullable and not column.primary_key
]


@pytest.mark.parametrize("model,field", REQUIRED)
def test_required_history_fields_reject_null(history_db, records, model, field):
    values = core_values(records[MODELS.index(model)])
    values["id"] = 900
    if model is ClaimIntake:
        values.update(public_id=str(uuid4()), idempotency_key=str(uuid4()))
    else:
        values["attempt_no" if model is ClaimValidationAttempt else "event_no"] = 2
    # SQL NULL must bypass Python defaults and JSON's JSON-null serialization.
    values.pop(field)
    with pytest.raises(sa.exc.IntegrityError):
        with history_db.kw["bind"].begin() as connection:
            connection.execute(
                model.__table__.insert().values(**values, **{field: sa.null()})
            )


@pytest.mark.parametrize(
    "model,field",
    [
        (ClaimIntake, "public_id"),
        (ClaimIntake, "idempotency_key"),
        (ClaimValidationAttempt, "revalidation_idempotency_key"),
    ],
)
def test_raw_uuid_inputs_must_be_canonical(history_db, records, model, field):
    values = core_values(records[MODELS.index(model)])
    values["id"] = 900
    if model is ClaimIntake:
        values.update(public_id=str(uuid4()), idempotency_key=str(uuid4()))
    else:
        values["attempt_no"] = 2
    for invalid in (
        "g" * 8 + "-0000-0000-0000-" + "0" * 12,
        uuid4().hex,
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    ):
        values[field] = invalid
        with pytest.raises(sa.exc.IntegrityError):
            with history_db.kw["bind"].begin() as connection:
                connection.execute(model.__table__.insert(), values)


@pytest.mark.parametrize(
    "model,field",
    [
        (ClaimIntake, "owner_user_id"),
        (ClaimValidationAttempt, "intake_id"),
        (ClaimValidationAttempt, "actor_user_id"),
        (ClaimIntakeEvent, "intake_id"),
        (ClaimIntakeEvent, "actor_user_id"),
    ],
)
def test_foreign_keys_restrict_unowned_parents_and_actors(
    history_db, records, model, field
):
    values = core_values(records[MODELS.index(model)])
    values["id"] = 900
    if model is ClaimIntake:
        values.update(public_id=str(uuid4()), idempotency_key=str(uuid4()))
    else:
        values["attempt_no" if model is ClaimValidationAttempt else "event_no"] = 2
    values[field] = 999999
    with pytest.raises(sa.exc.IntegrityError):
        with history_db.kw["bind"].begin() as connection:
            connection.execute(model.__table__.insert(), values)


def test_owner_soft_delete_preserves_history_and_physical_delete_is_restricted(
    history_db, records
):
    before = persisted(history_db)
    with history_db.begin() as db:
        db.get(User, 1).soft_delete()
    assert persisted(history_db) == before
    with pytest.raises(sa.exc.IntegrityError):
        with history_db.kw["bind"].begin() as connection:
            connection.exec_driver_sql("DELETE FROM users WHERE id = 1")
    assert persisted(history_db) == before


IMMUTABLE = [
    (model, column.name) for model in MODELS for column in model.__table__.columns
]


@pytest.mark.parametrize("model,field", IMMUTABLE)
def test_orm_rejects_every_persisted_field_update(history_db, records, model, field):
    before = persisted(history_db)
    with history_db() as db:
        row = db.get(model, records[MODELS.index(model)].id)
        value = getattr(row, field)
        column = model.__table__.c[field]
        if field in {"public_id", "idempotency_key", "revalidation_idempotency_key"}:
            changed = str(uuid4())
        elif field == "details":
            changed = {"attempt_no": 2}
        elif isinstance(column.type, sa.DateTime):
            changed = value + timedelta(seconds=1)
        elif isinstance(column.type, sa.Integer):
            changed = value + 1 if value is not None else 2
        elif isinstance(value, dict):
            changed = {"changed": True}
        else:
            changed = "changed"
        setattr(row, field, changed)
        with pytest.raises(ValueError, match="append-only"):
            db.flush()
        db.rollback()
    assert persisted(history_db) == before


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize(
    "operation",
    [
        "delete",
        "soft_delete",
        "restore",
        "orm_update",
        "query_update",
        "bulk_mapping",
        "orm_delete",
        "query_delete",
    ],
)
def test_orm_and_bulk_mutations_rejected(history_db, records, model, operation):
    before = persisted(history_db)
    with history_db() as db:
        row = db.get(model, records[MODELS.index(model)].id)
        with pytest.raises(
            (ValueError, sa.exc.DBAPIError), match="append-only|bulk deletes"
        ):
            if operation == "delete":
                db.delete(row)
                db.flush()
            elif operation in {"soft_delete", "restore"}:
                getattr(row, operation)()
            elif operation == "orm_update":
                db.execute(
                    sa.update(model),
                    [
                        {
                            "id": row.id,
                            (
                                "request_id"
                                if model is not ClaimIntake
                                else "schema_version"
                            ): "changed",
                        }
                    ],
                )
            elif operation == "query_update":
                db.query(model).filter(model.id == row.id).update({model.id: row.id})
            elif operation == "bulk_mapping":
                db.bulk_update_mappings(
                    model,
                    [
                        {
                            "id": row.id,
                            (
                                "request_id"
                                if model is not ClaimIntake
                                else "schema_version"
                            ): "changed",
                        }
                    ],
                )
            elif operation == "orm_delete":
                db.execute(sa.delete(model).where(model.id == row.id))
            else:
                db.query(model).filter(model.id == row.id).delete()
        db.rollback()
    assert persisted(history_db) == before


@pytest.mark.parametrize("model", MODELS)
def test_raw_database_mutations_rejected(history_db, records, model):
    before = persisted(history_db)
    engine = history_db.kw["bind"]
    statements = [
        f"UPDATE {model.__tablename__} SET id = id",
        f"DELETE FROM {model.__tablename__}",
    ]
    if engine.dialect.name == "postgresql":
        statements.append(f"TRUNCATE {model.__tablename__} CASCADE")
    for statement in statements:
        with pytest.raises(
            sa.exc.DBAPIError, match="Claim intake history is append-only"
        ):
            with engine.begin() as connection:
                connection.exec_driver_sql(statement)
        assert persisted(history_db) == before


@pytest.mark.parametrize(
    "details",
    [
        {"patient_name": "SYNTHETIC"},
        {"request": {}},
        {"token": "SYNTHETIC"},
        {"result": None},
        {"result": {}},
        {"attempt_no": 0},
        {"attempt_no": True},
        {"attempt_no": "1"},
        {"attempt_no": None},
        {"result": "OTHER"},
    ],
)
def test_event_metadata_is_controlled_at_orm_and_database(history_db, records, details):
    with pytest.raises(ValueError):
        ClaimIntakeEvent(**{**event_values(records[0].id, 2), "details": details})
    values = core_values(records[2])
    values.update(id=900, event_no=2, details=details)
    with pytest.raises(sa.exc.IntegrityError):
        with history_db.kw["bind"].begin() as connection:
            connection.execute(ClaimIntakeEvent.__table__.insert(), values)


@pytest.mark.parametrize(
    "model,field",
    [
        (ClaimIntake, "validated_submission_snapshot"),
        (ClaimValidationAttempt, "operation_outcome_snapshot"),
        (ClaimValidationAttempt, "validation_report_snapshot"),
    ],
)
@pytest.mark.parametrize("value", [None, [], "not an object"])
def test_json_snapshots_require_objects(history_db, records, model, field, value):
    values = core_values(records[MODELS.index(model)])
    values["id"] = 900
    if model is ClaimIntake:
        values.update(public_id=str(uuid4()), idempotency_key=str(uuid4()))
    else:
        values["attempt_no"] = 2
    values[field] = value
    with pytest.raises(sa.exc.IntegrityError):
        with history_db.kw["bind"].begin() as connection:
            connection.execute(model.__table__.insert(), values)


def test_original_snapshot_is_lossless_and_normalized_submission_revalidates(
    history_db, payload
):
    original = json.dumps(payload, ensure_ascii=False, indent=3) + "\n"
    submission = ClaimSubmission.model_validate(json.loads(original))
    normalized = submission.model_dump(mode="json")
    with history_db.begin() as db:
        users(db)
        values = intake_values()
        values.update(
            original_request_snapshot=original, validated_submission_snapshot=normalized
        )
        intake = ClaimIntake(**values)
        db.add(intake)
        db.flush()
        identifier = intake.id
    with history_db() as db:
        saved = db.get(ClaimIntake, identifier)
        assert saved.original_request_snapshot.encode("utf-8") == original.encode(
            "utf-8"
        )
        assert (
            ClaimSubmission.model_validate(saved.validated_submission_snapshot)
            == submission
        )


def test_phase11b1_report_and_outcome_roundtrip(report_db, payload, caplog, capsys):
    add_rule(report_db, False)
    submission = ClaimSubmission.model_validate(payload)
    with report_db.begin() as db:
        users(db)
        report = evaluate_report(db, submission)
        snapshot = json.loads(json.dumps(asdict(report)))
        outcome = {
            "resourceType": "OperationOutcome",
            "issue": [
                issue.model_dump(mode="json", exclude_none=True)
                for issue in report_issues(report)
            ],
        }
        values = intake_values()
        values.update(
            original_request_snapshot=json.dumps(payload),
            validated_submission_snapshot=submission.model_dump(mode="json"),
        )
        intake = ClaimIntake(**values)
        db.add(intake)
        db.flush()
        attempt = attempt_values(intake.id)
        attempt.update(
            result=report.result.value.upper(),
            reason="validation_" + report.result.value,
            operation_outcome_snapshot=outcome,
            validation_report_snapshot=snapshot,
        )
        db.add(ClaimValidationAttempt(**attempt))
    with report_db() as db:
        saved = db.scalar(sa.select(ClaimValidationAttempt))
        assert saved.validation_report_snapshot == snapshot
        assert saved.operation_outcome_snapshot == outcome
        assert saved.validation_report_snapshot["observations"][0]["business_result"][
            "coverage"
        ]["matched_rule_ids"]
    assert "Synthetic patient" not in caplog.text + capsys.readouterr().out


def test_insert_rollback_has_no_hidden_commit(history_db):
    with history_db.begin() as db:
        users(db)
    commits = []
    engine = history_db.kw["bind"]

    def committed(connection):
        commits.append(True)

    sa.event.listen(engine, "commit", committed)
    try:
        with pytest.raises(RuntimeError, match="outer rollback"):
            with history_db.begin() as db:
                history(db)
                assert db.in_transaction()
                raise RuntimeError("outer rollback")
        assert not commits
    finally:
        sa.event.remove(engine, "commit", committed)
    assert all(not rows for rows in persisted(history_db).values())


@pytest.mark.parametrize(
    "model,field",
    [
        (ClaimIntake, "validated_submission_snapshot"),
        (ClaimValidationAttempt, "validation_report_snapshot"),
        (ClaimIntakeEvent, "details"),
    ],
)
def test_marked_in_place_json_mutation_is_rejected(history_db, records, model, field):
    before = persisted(history_db)
    with history_db() as db:
        row = db.get(model, records[MODELS.index(model)].id)
        getattr(row, field)["attempt_no"] = 2
        flag_modified(row, field)
        with pytest.raises(ValueError, match="append-only"):
            db.flush()
        db.rollback()
    assert persisted(history_db) == before


@pytest.mark.parametrize("original", ["", "null", "[]", "not JSON"])
def test_original_snapshot_requires_json_object(history_db, records, original):
    values = core_values(records[0])
    values.update(
        id=900,
        public_id=str(uuid4()),
        idempotency_key=str(uuid4()),
        original_request_snapshot=original,
    )
    with pytest.raises(sa.exc.DBAPIError):
        with history_db.kw["bind"].begin() as connection:
            connection.execute(ClaimIntake.__table__.insert(), values)


def test_snapshots_do_not_serialize_orm_entities_or_log_contents(
    history_db, records, caplog, capsys
):
    before = persisted(history_db)
    with history_db() as db:
        values = attempt_values(records[0].id, 2)
        values["validation_report_snapshot"] = {"entity": db.get(User, 1)}
        db.add(ClaimValidationAttempt(**values))
        # SQLite serializes through SQLAlchemy; psycopg adapts JSON at its
        # driver boundary and propagates the same TypeError without wrapping.
        with pytest.raises((sa.exc.StatementError, TypeError)):
            db.flush()
        db.rollback()
    assert persisted(history_db) == before
    captured = caplog.text + capsys.readouterr().out
    assert records[0].original_request_snapshot not in captured
    assert "intake-owner" not in captured


def test_metadata_create_all_has_database_protection(history_engine):
    Base.metadata.create_all(history_engine)
    factory = sessionmaker(history_engine, expire_on_commit=False)
    with factory.begin() as db:
        users(db)
        history(db)
    for model in MODELS:
        test_raw_database_mutations_rejected(factory, None, model)


def test_migration_preserves_existing_data_and_refuses_downgrade(history_engine):
    command.upgrade(config(), PARENT)
    factory = sessionmaker(history_engine, expire_on_commit=False)
    with factory.begin() as db:
        users(db)
        diagnosis = DiagnosisCode(code="SYNTHETIC-D", description="Synthetic")
        service = ServiceCode(code="SYNTHETIC-S", description="Synthetic")
        db.add_all(
            [
                diagnosis,
                service,
                NphiesTerminology(code_system_url="urn:synthetic", code="TEST"),
            ]
        )
        db.flush()
        create_rule(
            db,
            diagnosis.id,
            service.id,
            True,
            context=MutationContext(
                reason="Synthetic migration preservation", actor_user_id=1
            ),
        )
    old_tables = [
        name
        for name in sa.inspect(history_engine).get_table_names()
        if name != "alembic_version"
    ]

    def old_data():
        with history_engine.connect() as connection:
            return {
                name: connection.execute(
                    sa.select(Base.metadata.tables[name]).order_by(
                        Base.metadata.tables[name].c.id
                    )
                ).all()
                for name in old_tables
            }

    before = old_data()
    command.upgrade(config(), HEAD)
    assert old_data() == before
    with history_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == HEAD
        )
        assert (
            compare_metadata(
                MigrationContext.configure(connection, opts={"compare_type": True}),
                Base.metadata,
            )
            == []
        )
    with factory.begin() as db:
        history(db)
    history_before = persisted(factory)
    for model in MODELS:
        test_raw_database_mutations_rejected(factory, None, model)
    with pytest.raises(RuntimeError, match="Claim intake history cannot be removed"):
        command.downgrade(config(), PARENT)
    assert persisted(factory) == history_before
    assert old_data() == before
    with history_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == HEAD
        )


def test_alembic_head_and_parent():
    script = ScriptDirectory(str(ROOT / "alembic"))
    assert script.get_heads() == [HEAD]
    assert script.get_revision(HEAD).down_revision == PARENT
