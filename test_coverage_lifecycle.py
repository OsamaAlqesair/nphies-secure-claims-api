"""Phase 10A-2A: behavioral tests and real disposable PostgreSQL verification."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

import services.coverage_mutations as mutations
from models import (
    Base,
    CoverageRuleHistory,
    DiagnosisCode,
    DiagnosisServiceRule,
    InsuranceCompany,
    ServiceCode,
    User,
)
from services.coverage import CoverageStatus, resolve_coverage
from services.coverage_mutations import (
    CoverageMutationError,
    MutationContext,
    create_rule,
    soft_delete_rule,
    restore_rule,
    update_rule_coverage,
)
from test_coverage_mutations import history_db, history
from test_terminology_identity import postgres_engine
from testing_coverage import historical_row


@pytest.fixture
def lifecycle_db(history_db):
    # The checkpoint fixture seeds explicit IDs. Advance the disposable sequence
    # so CREATE exercises generated IDs rather than collisions with those seeds.
    with history_db.begin() as db:
        db.add(
            ServiceCode(id=2, code="CREATE-S", description="Synthetic create service")
        )
        db.flush()
        if db.bind.dialect.name == "postgresql":
            db.connection().execute(
                sa.text(
                    "SELECT setval(pg_get_serial_sequence('diagnosis_service_rules', 'id'), "
                    "(SELECT max(id) FROM diagnosis_service_rules))"
                )
            )
            db.connection().execute(
                sa.text(
                    "SELECT setval(pg_get_serial_sequence('users', 'id'), "
                    "(SELECT max(id) FROM users))"
                )
            )
    return history_db


def context(**kwargs):
    return MutationContext(
        reason="  Synthetic lifecycle review  ",
        source="coverage_mutations.py",
        **kwargs,
    )


def state(factory):
    with factory() as db:
        rules = db.execute(
            sa.select(
                DiagnosisServiceRule.id,
                DiagnosisServiceRule.diagnosis_id,
                DiagnosisServiceRule.service_id,
                DiagnosisServiceRule.insurer_id,
                DiagnosisServiceRule.is_covered,
                DiagnosisServiceRule.is_deleted,
            )
            .order_by(DiagnosisServiceRule.id)
            .execution_options(include_deleted=True)
        ).all()
    return rules, [(h.rule_id, h.action) for h in history(factory)]


def invoke(db, operation, *, ctx=None):
    ctx = ctx or context()
    if operation == "create":
        return create_rule(db, 1, 2, True, context=ctx)
    if operation == "soft_delete":
        return soft_delete_rule(db, 1, context=ctx)
    return restore_rule(db, 3, context=ctx)


def prepare(factory, operation):
    if operation == "restore":
        with factory.begin() as db:
            soft_delete_rule(db, 2, context=context())


@pytest.mark.parametrize("insurer_id", [None, 1])
@pytest.mark.parametrize("coverage", [True, False])
def test_create_snapshot_and_duplicate_rejection(lifecycle_db, insurer_id, coverage):
    with lifecycle_db.begin() as db:
        first = create_rule(
            db, 1, 2, coverage, insurer_id=insurer_id, context=context(actor_user_id=1)
        )
        assert first.id > 3
        assert first.is_deleted is False and first.is_covered is coverage
        row = db.scalar(
            sa.select(CoverageRuleHistory).where(
                CoverageRuleHistory.rule_id == first.id
            )
        )
        assert row.action == "CREATE"
        assert (row.diagnosis_id, row.service_id, row.insurer_id) == (1, 2, insurer_id)
        assert (row.diagnosis_code, row.service_code) == ("TEST-D", "CREATE-S")
        assert row.insurer_name == ("Synthetic insurer" if insurer_id else None)
        assert row.old_is_covered is None and row.old_is_deleted is None
        assert row.new_is_covered is coverage and row.new_is_deleted is False
        assert row.actor_user_id == 1 and row.source == "coverage_mutations.py"
        assert (
            row.reason == "Synthetic lifecycle review" and row.occurred_at is not None
        )
    before = state(lifecycle_db)
    with pytest.raises(CoverageMutationError) as error:
        with lifecycle_db.begin() as db:
            create_rule(db, 1, 2, coverage, insurer_id=insurer_id, context=context())
    assert error.value.code == "rule_create_conflict"
    assert state(lifecycle_db) == before
    assert len(history(lifecycle_db)) == 1


@pytest.mark.parametrize("model", [DiagnosisCode, ServiceCode, InsuranceCompany])
@pytest.mark.parametrize("deleted", [False, True])
def test_create_requires_current_mappings(lifecycle_db, model, deleted):
    ids = {DiagnosisCode: 1, ServiceCode: 1, InsuranceCompany: 1}
    if deleted:
        with lifecycle_db.begin() as db:
            db.get(model, 1).soft_delete()
    else:
        ids[model] = 999
    before = state(lifecycle_db)
    with pytest.raises(CoverageMutationError) as error:
        with lifecycle_db.begin() as db:
            create_rule(
                db,
                ids[DiagnosisCode],
                ids[ServiceCode],
                False,
                insurer_id=ids[InsuranceCompany],
                context=context(),
            )
    assert error.value.code == (
        "rule_mapping_deleted" if deleted else "rule_mapping_missing"
    )
    assert state(lifecycle_db) == before


def test_soft_delete_retains_row_and_excludes_resolution(lifecycle_db):
    with lifecycle_db.begin() as db:
        assert resolve_coverage(db, 1, 1, None).matched_rule_ids == (1,)
        rule = soft_delete_rule(db, 1, context=context())
        assert rule.is_deleted is True and rule.is_covered is False
        assert (
            resolve_coverage(db, 1, 1, None).status == CoverageStatus.NO_APPLICABLE_RULE
        )
        assert (
            db.scalar(
                sa.select(DiagnosisServiceRule).where(DiagnosisServiceRule.id == 1)
            )
            is None
        )
        assert (
            db.scalar(
                sa.select(DiagnosisServiceRule)
                .where(DiagnosisServiceRule.id == 1)
                .execution_options(include_deleted=True)
            )
            is rule
        )
    row = history(lifecycle_db)[0]
    assert row.action == "SOFT_DELETE"
    assert row.old_is_deleted is False and row.new_is_deleted is True
    assert row.old_is_covered is False and row.new_is_covered is False


@pytest.mark.parametrize("model", [DiagnosisCode, ServiceCode, InsuranceCompany])
def test_soft_delete_snapshots_deleted_mapping(lifecycle_db, model):
    with lifecycle_db.begin() as db:
        db.get(model, 1).soft_delete()
    with lifecycle_db.begin() as db:
        soft_delete_rule(db, 2, context=context())
    row = history(lifecycle_db)[0]
    assert (row.diagnosis_code, row.service_code, row.insurer_name) == (
        "TEST-D",
        "TEST-S",
        "Synthetic insurer",
    )
    assert row.old_is_covered is False and row.new_is_covered is False


@pytest.mark.parametrize(
    "operation,rule_id,code",
    [
        (soft_delete_rule, 999, "rule_not_found"),
        (soft_delete_rule, 3, "rule_already_deleted"),
        (restore_rule, 999, "rule_not_found"),
        (restore_rule, 1, "rule_already_current"),
    ],
)
def test_lifecycle_target_errors(lifecycle_db, operation, rule_id, code):
    before = state(lifecycle_db)
    with pytest.raises(CoverageMutationError) as error:
        with lifecycle_db.begin() as db:
            operation(db, rule_id, context=context())
    assert error.value.code == code
    assert state(lifecycle_db) == before


@pytest.mark.parametrize("insurer_id", [None, 1])
@pytest.mark.parametrize("coverage", [True, False])
def test_restore_success_and_repeat(lifecycle_db, insurer_id, coverage):
    with lifecycle_db.begin() as db:
        current_id = 1 if insurer_id is None else 2
        soft_delete_rule(db, current_id, context=context())
        rule = create_rule(db, 1, 1, coverage, insurer_id=insurer_id, context=context())
        soft_delete_rule(db, rule.id, context=context())
    with lifecycle_db.begin() as db:
        restored = restore_rule(db, rule.id, context=context())
        assert restored.is_deleted is False and restored.is_covered is coverage
        assert rule.id in resolve_coverage(db, 1, 1, insurer_id).matched_rule_ids
    row = history(lifecycle_db)[-1]
    assert row.action == "RESTORE" and row.rule_id == rule.id
    assert (row.diagnosis_code, row.service_code) == ("TEST-D", "TEST-S")
    assert row.insurer_id == insurer_id
    assert row.insurer_name == ("Synthetic insurer" if insurer_id else None)
    assert row.old_is_deleted is True and row.new_is_deleted is False
    assert row.old_is_covered is coverage and row.new_is_covered is coverage
    before = state(lifecycle_db)
    with pytest.raises(CoverageMutationError) as error:
        with lifecycle_db.begin() as db:
            restore_rule(db, rule.id, context=context())
    assert error.value.code == "rule_already_current"
    assert state(lifecycle_db) == before


@pytest.mark.parametrize(
    "target_scope,other_scope,conflict",
    [
        (None, None, True),
        (1, 1, True),
        (1, 2, False),
        (None, 1, False),
        (1, None, False),
    ],
)
def test_restore_conflict_scope(lifecycle_db, target_scope, other_scope, conflict):
    with lifecycle_db.begin() as db:
        # Remove the fixture's current rules, retaining deleted duplicates.
        soft_delete_rule(db, 1, context=context())
        soft_delete_rule(db, 2, context=context())
        db.add(InsuranceCompany(id=2, name="Another synthetic insurer"))
        db.flush()
        target = create_rule(db, 1, 1, True, insurer_id=target_scope, context=context())
        soft_delete_rule(db, target.id, context=context())
        create_rule(db, 1, 1, False, insurer_id=other_scope, context=context())
    before = state(lifecycle_db)
    if conflict:
        with pytest.raises(CoverageMutationError) as error:
            with lifecycle_db.begin() as db:
                restore_rule(db, target.id, context=context())
        assert error.value.code == "rule_restore_conflict"
        assert state(lifecycle_db) == before
    else:
        with lifecycle_db.begin() as db:
            restore_rule(db, target.id, context=context())
        assert history(lifecycle_db)[-1].action == "RESTORE"


@pytest.mark.parametrize("model", [DiagnosisCode, ServiceCode, InsuranceCompany])
def test_restore_rejects_deleted_mapping(lifecycle_db, model):
    prepare(lifecycle_db, "restore")
    with lifecycle_db.begin() as db:
        db.get(model, 1).soft_delete()
    before = state(lifecycle_db)
    with pytest.raises(CoverageMutationError) as error:
        with lifecycle_db.begin() as db:
            restore_rule(db, 3, context=context())
    assert error.value.code == "rule_mapping_deleted"
    assert state(lifecycle_db) == before


@pytest.mark.parametrize("operation", ["soft_delete", "restore"])
@pytest.mark.parametrize("model", [DiagnosisCode, ServiceCode, InsuranceCompany])
def test_lifecycle_missing_mapping_fails_safely(
    lifecycle_db, monkeypatch, operation, model
):
    prepare(lifecycle_db, operation)
    load = mutations._load_including_deleted

    def missing(db, requested_model, row_id):
        # Simulate a physically absent FK mapping without corrupting PG constraints.
        return None if requested_model is model else load(db, requested_model, row_id)

    monkeypatch.setattr(mutations, "_load_including_deleted", missing)
    before = state(lifecycle_db)
    with pytest.raises(CoverageMutationError) as error:
        with lifecycle_db.begin() as db:
            # Insurer-specific targets exercise all three mapping references.
            if operation == "soft_delete":
                soft_delete_rule(db, 2, context=context())
            else:
                restore_rule(db, 3, context=context())
    assert error.value.code == "rule_mapping_missing"
    assert state(lifecycle_db) == before


@pytest.mark.parametrize("operation", [soft_delete_rule, restore_rule])
@pytest.mark.parametrize("column", ["diagnosis_id", "service_id", "insurer_id"])
def test_physically_missing_mapping(lifecycle_db, operation, column):
    # Construct an orphan only in this fixture's disposable DB/schema. This
    # verifies actual database reads, complementing the missing-read fault test.
    engine = lifecycle_db.kw["bind"]
    values = dict(
        id=88,
        diagnosis_id=1,
        service_id=1,
        insurer_id=1,
        is_covered=True,
        is_deleted=operation is restore_rule,
    )
    values[column] = 999
    if engine.dialect.name == "postgresql":
        fk = next(
            fk
            for fk in sa.inspect(engine).get_foreign_keys("diagnosis_service_rules")
            if fk["constrained_columns"] == [column]
        )
        constraint = engine.dialect.identifier_preparer.quote(fk["name"])
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"ALTER TABLE diagnosis_service_rules DROP CONSTRAINT {constraint}"
            )
            historical_row(connection, **values)
    else:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            historical_row(connection, **values)
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
    before = state(lifecycle_db)
    with pytest.raises(CoverageMutationError) as error:
        with lifecycle_db.begin() as db:
            operation(db, 88, context=context())
    assert error.value.code == "rule_mapping_missing"
    assert state(lifecycle_db) == before


@pytest.mark.parametrize("operation", ["create", "restore"])
@pytest.mark.parametrize("model", [DiagnosisCode, ServiceCode, InsuranceCompany])
def test_cached_mapping_refreshed_before_reactivation(lifecycle_db, operation, model):
    prepare(lifecycle_db, operation)
    before = state(lifecycle_db)
    with lifecycle_db() as db:
        cached = db.get(model, 1)
        assert cached.is_deleted is False
        with lifecycle_db.kw["bind"].begin() as connection:
            connection.execute(
                model.__table__.update().where(model.id == 1).values(is_deleted=True)
            )
        with pytest.raises(CoverageMutationError) as error:
            if operation == "create":
                create_rule(db, 1, 1, True, insurer_id=1, context=context())
            else:
                restore_rule(db, 3, context=context())
        assert error.value.code == "rule_mapping_deleted"
        assert cached.is_deleted is True
        db.rollback()
    assert state(lifecycle_db) == before


@pytest.mark.parametrize("operation", ["create", "soft_delete", "restore"])
def test_history_failure_rolls_back_and_sanitizes(lifecycle_db, operation, caplog):
    prepare(lifecycle_db, operation)
    before = state(lifecycle_db)
    engine = lifecycle_db.kw["bind"]

    def reject(connection, cursor, statement, parameters, ctx, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO COVERAGE_RULE_HISTORY"):
            raise sa.exc.IntegrityError(
                "PRIVATE_SQL",
                {"secret": "PRIVATE_VALUE"},
                RuntimeError("PRIVATE_CREDENTIAL"),
            )

    sa.event.listen(engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(CoverageMutationError) as error:
            with lifecycle_db.begin() as db:
                invoke(db, operation)
    finally:
        sa.event.remove(engine, "before_cursor_execute", reject)
    assert error.value.code == "rule_mutation_failed"
    assert "PRIVATE_" not in str(error.value) + caplog.text
    record = caplog.records[-1]
    assert (
        record.getMessage()
        == f"coverage_mutation_{operation}_failed exception_type=IntegrityError"
    )
    assert record.exc_info is None and record.stack_info is None
    assert state(lifecycle_db) == before


@pytest.mark.parametrize("operation", ["create", "soft_delete", "restore"])
def test_outer_rollback_and_no_internal_commit(lifecycle_db, operation):
    prepare(lifecycle_db, operation)
    before = state(lifecycle_db)
    with pytest.raises(RuntimeError, match="outer rollback"):
        with lifecycle_db.begin() as db:
            invoke(db, operation)
            assert db.in_transaction()
            raise RuntimeError("outer rollback")
    assert state(lifecycle_db) == before


@pytest.mark.parametrize("operation", ["create", "soft_delete", "restore"])
@pytest.mark.parametrize("actor_state", ["missing", "deleted"])
def test_invalid_actor(lifecycle_db, operation, actor_state):
    prepare(lifecycle_db, operation)
    if actor_state == "deleted":
        with lifecycle_db.begin() as db:
            db.get(User, 1).soft_delete()
    before = state(lifecycle_db)
    with pytest.raises(CoverageMutationError) as error:
        with lifecycle_db.begin() as db:
            invoke(
                db,
                operation,
                ctx=context(actor_user_id=999 if actor_state == "missing" else 1),
            )
    assert error.value.code == "rule_actor_invalid"
    assert state(lifecycle_db) == before


@pytest.mark.parametrize("operation", ["create", "soft_delete", "restore"])
def test_requires_transaction(lifecycle_db, operation):
    with lifecycle_db() as db:
        with pytest.raises(CoverageMutationError, match="caller-owned"):
            invoke(db, operation)


@pytest.mark.parametrize("operation", ["create", "soft_delete", "restore"])
@pytest.mark.parametrize("model", [DiagnosisServiceRule, DiagnosisCode, User])
def test_pending_changes_not_overwritten_or_flushed(lifecycle_db, operation, model):
    prepare(lifecycle_db, operation)
    before = state(lifecycle_db)
    # Default autoflush is deliberately ON.
    with pytest.raises(CoverageMutationError) as error:
        with lifecycle_db.begin() as db:
            obj = db.get(model, 1)
            if model is DiagnosisServiceRule:
                obj.is_covered = True
            elif model is DiagnosisCode:
                obj.code = "PRIVATE_PENDING"
            else:
                obj.is_deleted = True
            invoke(db, operation, ctx=context(actor_user_id=1))
    assert error.value.code == "rule_pending_changes"
    assert state(lifecycle_db) == before


@pytest.mark.parametrize(
    "source", [None, "", "  ", "unknown", " coverage_mutations.py "]
)
def test_invalid_source_rejected(source):
    with pytest.raises(CoverageMutationError, match="source"):
        MutationContext(reason="Synthetic", source=source)


@pytest.mark.parametrize("actor", [True, 0, -1, "1"])
def test_actor_type_invalid(actor):
    with pytest.raises(CoverageMutationError) as error:
        context(actor_user_id=actor)
    assert error.value.code == "rule_actor_invalid"


@pytest.mark.parametrize("value", [None, 1, 0, "true"])
def test_create_boolean_required(lifecycle_db, value):
    before = state(lifecycle_db)
    with pytest.raises(CoverageMutationError, match="boolean"):
        with lifecycle_db.begin() as db:
            create_rule(db, 1, 1, value, context=context())
    assert state(lifecycle_db) == before


@pytest.mark.parametrize("history_db", ["postgresql"], indirect=True)
@pytest.mark.parametrize(
    "scope,same_rule", [(None, False), (1, False), (None, True), (1, True)]
)
def test_postgres_concurrent_restores(lifecycle_db, scope, same_rule):
    with lifecycle_db.begin() as db:
        assert db.connection().get_isolation_level() == "READ COMMITTED"
        soft_delete_rule(db, 1 if scope is None else 2, context=context())
        one = create_rule(db, 1, 1, True, insurer_id=scope, context=context())
        soft_delete_rule(db, one.id, context=context())
        two = create_rule(db, 1, 1, False, insurer_id=scope, context=context())
        soft_delete_rule(db, two.id, context=context())
    barrier = Barrier(2)

    def restore(rule_id):
        try:
            with lifecycle_db.begin() as db:
                # Stale identity-map entries must be refreshed after lock wait.
                cached = db.scalar(
                    sa.select(DiagnosisServiceRule)
                    .where(DiagnosisServiceRule.id == rule_id)
                    .execution_options(include_deleted=True)
                )
                assert cached.is_deleted is True
                db.connection().execute(sa.text("SET LOCAL lock_timeout = '8s'"))
                barrier.wait(timeout=10)
                restore_rule(db, rule_id, context=context())
            return "success"
        except CoverageMutationError as error:
            return error.code

    ids = [one.id, one.id if same_rule else two.id]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(restore, ids))
    assert sorted(results) == sorted(
        ["success", "rule_already_current" if same_rule else "rule_restore_conflict"]
    )
    rows = [h for h in history(lifecycle_db) if h.action == "RESTORE"]
    assert len(rows) == 1 and rows[0].rule_id in ids
    with lifecycle_db() as db:
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(DiagnosisServiceRule)
                .where(
                    DiagnosisServiceRule.id.in_(ids),
                    DiagnosisServiceRule.is_deleted.is_(False),
                )
            )
            == 1
        )


@pytest.mark.parametrize("history_db", ["postgresql"], indirect=True)
@pytest.mark.parametrize("failure", ["conflict", "history"])
def test_postgres_lock_released_after_failure(lifecycle_db, failure):
    locked, attempted = Event(), Event()
    engine = lifecycle_db.kw["bind"]
    if failure == "history":

        def reject(connection, cursor, statement, parameters, ctx, executemany):
            if (
                statement.lstrip()
                .upper()
                .startswith("INSERT INTO COVERAGE_RULE_HISTORY")
            ):
                if connection.info.get("reject_history"):
                    raise sa.exc.IntegrityError(
                        "PRIVATE_SQL", {}, RuntimeError("PRIVATE_VALUE")
                    )

        sa.event.listen(engine, "before_cursor_execute", reject)

    def fail():
        try:
            with lifecycle_db.begin() as db:
                mutations.lock_coverage_mutations(db)
                locked.set()
                assert attempted.wait(timeout=10)
                if failure == "history":
                    connection = db.connection()
                    connection.info["reject_history"] = True
                    try:
                        soft_delete_rule(db, 1, context=context())
                    finally:
                        connection.info.pop("reject_history", None)
                else:
                    restore_rule(db, 3, context=context())
        except CoverageMutationError as error:
            return error.code

    def proceed():
        assert locked.wait(timeout=10)
        with lifecycle_db.begin() as db:
            db.connection().execute(sa.text("SET LOCAL lock_timeout = '8s'"))
            attempted.set()
            create_rule(db, 1, 2, False, context=context())
        return "success"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(fail), pool.submit(proceed)
            assert first.result(timeout=15) == (
                "rule_restore_conflict"
                if failure == "conflict"
                else "rule_mutation_failed"
            )
            assert second.result(timeout=15) == "success"
    finally:
        if failure == "history":
            sa.event.remove(engine, "before_cursor_execute", reject)
    assert [h.action for h in history(lifecycle_db)] == ["CREATE"]


@pytest.mark.parametrize(
    "history_db",
    [
        ("sqlite", "0009_coverage_rule_current_identity"),
        ("postgresql", "0009_coverage_rule_current_identity"),
    ],
    indirect=True,
)
def test_history_source_migration_preserves_events_and_schema(lifecycle_db):
    cfg = Config(str(Path(__file__).with_name("alembic.ini")))
    engine = lifecycle_db.kw["bind"]
    command.downgrade(cfg, "0006_coverage_rule_history")
    with lifecycle_db.begin() as db:
        update_rule_coverage(
            db, 1, True, context=MutationContext(reason="Synthetic prior event")
        )
    with engine.connect() as connection:
        before_events = connection.execute(
            sa.select(CoverageRuleHistory.__table__)
        ).all()
        before_rules = connection.execute(
            sa.select(DiagnosisServiceRule.__table__)
        ).all()
    command.upgrade(cfg, "0007_coverage_history_sources")
    with engine.connect() as connection:
        # Compare the schema that existed before the intake-history migration.
        checkpoint = MigrationContext.configure(
            connection,
            opts={
                "include_object": lambda obj, name, kind, reflected, compared: kind
                != "table"
                or name
                not in {
                    "claim_intakes",
                    "claim_validation_attempts",
                    "claim_intake_events",
                }
            },
        )
        assert (
            before_events
            == connection.execute(sa.select(CoverageRuleHistory.__table__)).all()
        )
        assert (
            before_rules
            == connection.execute(sa.select(DiagnosisServiceRule.__table__)).all()
        )
        assert (
            all(
                diff[0] == "add_index"
                and diff[1].name in mutations._CURRENT_IDENTITY_INDEXES
                for diff in compare_metadata(checkpoint, Base.metadata)
            )
            and len(compare_metadata(checkpoint, Base.metadata)) == 2
        )
    inspector = sa.inspect(engine)
    assert inspector.get_unique_constraints("diagnosis_service_rules") == []
    assert not any(
        index["unique"] for index in inspector.get_indexes("diagnosis_service_rules")
    )
    # Old-source events alone allow a lossless downgrade, including SQLite's
    # table copy. Neither direction is allowed to rewrite even one event.
    command.downgrade(cfg, "0006_coverage_rule_history")
    with engine.connect() as connection:
        assert (
            before_events
            == connection.execute(sa.select(CoverageRuleHistory.__table__)).all()
        )
    command.upgrade(cfg, "0007_coverage_history_sources")
    with lifecycle_db.begin() as db:
        create_rule(db, 1, 2, False, context=context())
    preserved = state(lifecycle_db)
    with pytest.raises(RuntimeError, match="history must be preserved"):
        command.downgrade(cfg, "0006_coverage_rule_history")
    assert state(lifecycle_db) == preserved
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0007_coverage_history_sources"
        )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE coverage_rule_history SET reason = 'tampered'",
        "DELETE FROM coverage_rule_history",
        "TRUNCATE coverage_rule_history",
    ],
)
def test_history_protection_after_source_migration(lifecycle_db, statement):
    engine = lifecycle_db.kw["bind"]
    if statement.startswith("TRUNCATE") and engine.dialect.name != "postgresql":
        pytest.skip("SQLite has no TRUNCATE; PostgreSQL verifies it.")
    with lifecycle_db.begin() as db:
        create_rule(db, 1, 2, True, context=context())
    before = state(lifecycle_db)
    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        with engine.begin() as connection:
            connection.exec_driver_sql(statement)
    assert state(lifecycle_db) == before


def test_offline_source_downgrade_refused(lifecycle_db):
    cfg = Config(str(Path(__file__).with_name("alembic.ini")))
    with pytest.raises(RuntimeError, match="online inspection"):
        command.downgrade(
            cfg, "0007_coverage_history_sources:0006_coverage_rule_history", sql=True
        )


def test_pending_unreferenced_actor_does_not_block(lifecycle_db):
    with lifecycle_db.begin() as db:
        db.add(
            User(username="another-synthetic", role="admin", password_hash="test-only")
        )
        rule = create_rule(db, 1, 2, True, context=context())
    row = history(lifecycle_db)[0]
    assert row.rule_id == rule.id and row.actor_user_id is None


def test_unknown_history_source_rejected(lifecycle_db):
    with lifecycle_db.begin() as db:
        rule = create_rule(db, 1, 2, True, context=context())
    before = state(lifecycle_db)
    with pytest.raises(sa.exc.IntegrityError):
        with lifecycle_db.kw["bind"].begin() as connection:
            connection.execute(
                CoverageRuleHistory.__table__.insert().values(
                    rule_id=rule.id,
                    action="CREATE",
                    diagnosis_id=1,
                    service_id=1,
                    diagnosis_code="TEST-D",
                    service_code="TEST-S",
                    new_is_covered=True,
                    new_is_deleted=False,
                    reason="Synthetic",
                    source="unknown",
                )
            )
    assert state(lifecycle_db) == before
