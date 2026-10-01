"""ORM enforcement and audit completion in disposable SQLite/PostgreSQL."""

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from models import (
    DiagnosisServiceRule,
    DiagnosisCode,
    ServiceCode,
    InsuranceCompany,
    CoverageRuleHistory,
)
from services import coverage_write_guards as guards
from services import coverage_mutations as mutations
from services.coverage_mutations import CoverageMutationError, MutationContext
from test_coverage_writers import writer_db, mappings
from test_terminology_identity import postgres_engine


def context():
    return MutationContext(
        reason="Synthetic guard verification", source="coverage_mutations.py"
    )


@pytest.fixture
def guarded(writer_db):
    mappings(writer_db)
    with writer_db.begin() as db:
        db.add(ServiceCode(id=18, code="UNUSED-S", description="Synthetic"))
    with writer_db.begin() as db:
        rule = mutations.create_rule(db, 7, 8, False, context=context())
        other = mutations.create_rule(db, 7, 8, False, insurer_id=9, context=context())
    return writer_db, rule.id, other.id


def counts(factory):
    with factory() as db:
        return tuple(
            db.scalar(
                sa.select(sa.func.count())
                .select_from(model)
                .execution_options(include_deleted=True)
            )
            for model in (DiagnosisServiceRule, CoverageRuleHistory)
        )


@pytest.mark.parametrize("method", ["add", "add_all", "partial"])
def test_direct_create_rejected(guarded, method):
    factory, _, _ = guarded
    before = counts(factory)
    with factory() as db:
        rule = DiagnosisServiceRule(diagnosis_id=7, service_id=8, is_covered=True)
        if method == "add_all":
            db.add_all([rule, DiagnosisServiceRule(diagnosis_id=7, service_id=8)])
        else:
            db.add(rule)
        with pytest.raises(CoverageMutationError, match="audited mutation"):
            if method == "partial":
                unrelated = DiagnosisCode(code="OTHER", description="Synthetic")
                db.add(unrelated)
                db.flush([unrelated])
            else:
                db.flush()
        db.rollback()
    assert counts(factory) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("is_covered", True),
        ("is_deleted", True),
        ("diagnosis_id", 999),
        ("service_id", 999),
        ("insurer_id", 9),
        ("id", 999),
        ("created_at", datetime(2001, 1, 1, tzinfo=timezone.utc)),
        ("updated_at", datetime(2001, 1, 1, tzinfo=timezone.utc)),
    ],
)
def test_direct_column_changes_rejected(guarded, field, value):
    factory, rule_id, _ = guarded
    with factory() as db:
        rule = db.get(DiagnosisServiceRule, rule_id)
        setattr(rule, field, value)
        with pytest.raises(CoverageMutationError, match="audited mutation"):
            db.flush()
        db.rollback()


@pytest.mark.parametrize(
    "name,model",
    [
        ("diagnosis", DiagnosisCode),
        ("service", ServiceCode),
        ("insurer", InsuranceCompany),
    ],
)
def test_relationship_reassignment_rejected(guarded, name, model):
    factory, rule_id, _ = guarded
    with factory.begin() as db:
        values = (
            {"name": "Other"}
            if model is InsuranceCompany
            else {"code": "OTHER", "description": "Synthetic"}
        )
        related = model(**values)
        db.add(related)
    with factory() as db:
        rule = db.get(DiagnosisServiceRule, rule_id)
        original = getattr(rule, name + "_id")
        setattr(rule, name, db.get(model, related.id))
        assert getattr(rule, name + "_id") == original
        with pytest.raises(CoverageMutationError):
            db.flush()
        db.rollback()


@pytest.mark.parametrize("method", ["soft_delete", "restore", "delete"])
def test_lifecycle_shortcuts_rejected(guarded, method):
    factory, rule_id, _ = guarded
    with factory() as db:
        rule = db.get(DiagnosisServiceRule, rule_id)
        with pytest.raises(CoverageMutationError):
            if method == "delete":
                db.delete(rule)
                db.flush()
            else:
                getattr(rule, method)()
        assert rule.is_deleted is False
        db.rollback()


def test_merge_rejected(guarded):
    factory, rule_id, _ = guarded
    with factory() as db:
        rule = db.get(DiagnosisServiceRule, rule_id)
        db.expunge(rule)
    rule.is_covered = True
    with factory() as db:
        db.merge(rule)
        with pytest.raises(CoverageMutationError):
            db.flush()
        db.rollback()


@pytest.mark.parametrize("kind", ["insert", "update", "delete"])
@pytest.mark.parametrize("target", ["orm", "table", "reflected"])
def test_session_dml_rejected(guarded, kind, target):
    factory, rule_id, _ = guarded
    model = DiagnosisServiceRule if target == "orm" else DiagnosisServiceRule.__table__
    if target == "reflected":
        model = sa.Table(
            "diagnosis_service_rules", sa.MetaData(), autoload_with=factory.kw["bind"]
        )
    statement = getattr(sa, kind)(model)
    if kind == "insert":
        statement = statement.values(diagnosis_id=7, service_id=8, is_covered=True)
    elif kind == "update":
        statement = statement.values(is_covered=True)
    with factory() as db:
        with pytest.raises(CoverageMutationError):
            db.execute(statement)
        db.rollback()


def test_harmless_dirty_and_expired_ambiguous_state(guarded):
    factory, rule_id, _ = guarded
    with factory.begin() as db:
        rule = db.get(DiagnosisServiceRule, rule_id)
        rule.is_covered = False
        db.flush()
    with factory() as db:
        rule = db.get(DiagnosisServiceRule, rule_id)
        db.expire(rule, ["is_covered"])
        rule.is_covered = False
        with pytest.raises(CoverageMutationError):
            db.flush()
        db.rollback()


def test_sequential_services_and_noop(guarded):
    factory, rule_id, _ = guarded
    with factory.begin() as db:
        assert (
            mutations.update_rule_coverage(db, rule_id, False, context=context())
            is False
        )
        assert (
            mutations.update_rule_coverage(db, rule_id, True, context=context()) is True
        )
        mutations.soft_delete_rule(db, rule_id, context=context())
        mutations.restore_rule(db, rule_id, context=context())
        imported = mutations.import_rule(
            db, 88, 7, 8, True, is_deleted=True, context=context()
        )
        assert imported.is_deleted is True
        assert db not in guards._permits and db not in guards._entries
    assert counts(factory) == (3, 6)


@pytest.mark.parametrize("operation", ["create", "update", "delete", "restore", "noop"])
def test_unrelated_pending_rule_blocks_service(guarded, operation):
    factory, rule_id, other_id = guarded
    if operation == "restore":
        with factory.begin() as db:
            mutations.soft_delete_rule(db, rule_id, context=context())
    with factory() as db:
        db.begin()
        db.get(DiagnosisServiceRule, other_id).is_covered = True
        with pytest.raises(CoverageMutationError, match="pending changes"):
            if operation == "create":
                mutations.create_rule(db, 7, 8, True, context=context())
            elif operation in ("update", "noop"):
                mutations.update_rule_coverage(
                    db, rule_id, operation == "update", context=context()
                )
            elif operation == "delete":
                mutations.soft_delete_rule(db, rule_id, context=context())
            else:
                mutations.restore_rule(db, rule_id, context=context())
        assert db not in guards._permits and db not in guards._entries
        db.rollback()


@pytest.mark.parametrize(
    "attack",
    ["other_rule", "other_session", "reentrant", "execute", "extra_field", "history"],
)
def test_operation_authorization_isolation(guarded, monkeypatch, attack):
    factory, rule_id, other_id = guarded
    original = guards._before_flush
    attempted = False

    def attacked(db):
        nonlocal attempted
        permit = guards._permits.get(db)
        if permit and not attempted:
            attempted = True
            if attack == "other_rule":
                with db.no_autoflush:
                    db.get(DiagnosisServiceRule, other_id).is_covered = True
            elif attack == "other_session":
                with factory() as other:
                    other.add(DiagnosisServiceRule(diagnosis_id=7, service_id=8))
                    other.flush()
            elif attack == "reentrant":
                mutations.update_rule_coverage(db, other_id, True, context=context())
            elif attack == "execute":
                db.execute(sa.update(DiagnosisServiceRule).values(is_covered=True))
            elif attack == "extra_field":
                permit.rule.insurer_id = 9
            else:
                permit.history.reason = "Changed expected history"
        original(db)

    import models

    monkeypatch.setattr(models, "_before_flush", attacked)
    with factory() as db:
        db.begin()
        with pytest.raises(CoverageMutationError):
            mutations.update_rule_coverage(db, rule_id, True, context=context())
        assert attempted and db not in guards._permits and db not in guards._entries
        db.rollback()
    assert counts(factory) == (2, 2)


@pytest.mark.parametrize("joined", [False, True])
@pytest.mark.parametrize("savepoint", [False, True])
@pytest.mark.parametrize("operation", ["create", "import"])
def test_incomplete_history_rejects_commit_until_rollback(
    guarded, monkeypatch, joined, savepoint, operation
):
    factory, _, _ = guarded
    engine = factory.kw["bind"]
    original = Session.add

    def fail_history(db, obj, **kwargs):
        if isinstance(obj, CoverageRuleHistory):
            raise RuntimeError("Synthetic failure after rule SQL")
        return original(db, obj, **kwargs)

    connection = engine.connect() if joined else None
    outer = connection.begin() if joined else None
    db = (
        Session(bind=connection, join_transaction_mode="rollback_only")
        if joined
        else factory()
    )
    try:
        db.begin()
        nested = db.begin_nested() if savepoint else None
        with monkeypatch.context() as change:
            change.setattr(Session, "add", fail_history)
            with pytest.raises(RuntimeError, match="after rule SQL"):
                if operation == "create":
                    mutations.create_rule(db, 7, 18, True, context=context())
                else:
                    mutations.import_rule(db, 99, 7, 18, True, context=context())
        assert db not in guards._permits and db not in guards._entries
        with pytest.raises(CoverageMutationError, match="incomplete"):
            db.commit()
        if nested is not None:
            nested.rollback()
        if joined:
            # A joined Session ending must not erase the real outer obligation.
            with pytest.raises(CoverageMutationError, match="incomplete"):
                connection.commit()
            connection.rollback()
            db.close()
        else:
            db.rollback()
    finally:
        db.close()
        if connection is not None:
            connection.rollback()
            connection.close()
    assert counts(factory) == (2, 2)
    with factory.begin() as fresh:
        mutations.create_rule(fresh, 7, 18, True, context=context())
    assert counts(factory) == (3, 3)


def test_unrelated_generic_lifecycle_unchanged(guarded):
    factory, _, _ = guarded
    with factory.begin() as db:
        obj = DiagnosisCode(code="UNRELATED", description="Synthetic")
        db.add(obj)
    with factory.begin() as db:
        obj = db.get(DiagnosisCode, obj.id)
        obj.description = "Updated"
        obj.soft_delete()
    with factory.begin() as db:
        obj = db.scalar(
            sa.select(DiagnosisCode)
            .where(DiagnosisCode.id == obj.id)
            .execution_options(include_deleted=True)
        )
        obj.restore()
    with factory.begin() as db:
        obj = db.get(DiagnosisCode, obj.id)
        db.delete(obj)
    with factory() as db:
        obj = db.scalar(
            sa.select(DiagnosisCode)
            .where(DiagnosisCode.id == obj.id)
            .execution_options(include_deleted=True)
        )
        assert obj.is_deleted and obj.description == "Updated"


def test_stale_permit_cannot_cross_transaction(guarded, monkeypatch):
    factory, rule_id, _ = guarded
    captured = []
    import models

    original = models._before_flush

    def capture(db):
        if db in guards._permits:
            captured.append(guards._permits[db])
        original(db)

    monkeypatch.setattr(models, "_before_flush", capture)
    with factory() as db:
        with db.begin():
            mutations.update_rule_coverage(db, rule_id, True, context=context())
        permit = captured[0]
        db.begin()
        rule = db.get(DiagnosisServiceRule, rule_id)
        rule.is_covered = False
        # Even possession of the old private permit cannot revive its transaction.
        guards._permits[db] = permit
        guards._entries[db] = permit.transaction
        try:
            with pytest.raises(CoverageMutationError):
                db.flush()
        finally:
            guards._permits.pop(db, None)
            guards._entries.pop(db, None)
            db.rollback()


def test_joined_session_close_preserves_incomplete_obligation(guarded, monkeypatch):
    factory, _, _ = guarded
    original = Session.add

    def fail(db, obj, **kwargs):
        if isinstance(obj, CoverageRuleHistory):
            raise RuntimeError("Synthetic missing history")
        return original(db, obj, **kwargs)

    with factory.kw["bind"].connect() as connection:
        connection.begin()
        with Session(bind=connection, join_transaction_mode="rollback_only") as db:
            db.begin()
            with monkeypatch.context() as change:
                change.setattr(Session, "add", fail)
                with pytest.raises(RuntimeError):
                    mutations.create_rule(db, 7, 18, True, context=context())
        assert connection.in_transaction()
        with pytest.raises(CoverageMutationError, match="incomplete"):
            connection.commit()
        connection.rollback()
    assert counts(factory) == (2, 2)


def test_setup_helper_requires_live_fixture_ownership():
    from testing_coverage import owned_sqlite, historical_rules, historical_change
    from models import Base

    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            db.begin()
            row = DiagnosisServiceRule(
                id=20, diagnosis_id=1, service_id=1, is_covered=False
            )
            with pytest.raises(ValueError, match="Unregistered"):
                historical_rules(db, row)
            with owned_sqlite(engine):
                historical_rules(db, row)
                db.flush()
                stored = db.get(DiagnosisServiceRule, 20)
                historical_change(db, stored, is_deleted=True)
                # The helper never enables ordinary ORM writes.
                db.add(DiagnosisServiceRule(diagnosis_id=1, service_id=1))
                with pytest.raises(CoverageMutationError):
                    db.flush()
                db.expunge_all()
            with pytest.raises(ValueError, match="Unregistered"):
                historical_rules(
                    db, DiagnosisServiceRule(id=21, diagnosis_id=1, service_id=1)
                )
            db.rollback()
    finally:
        engine.dispose()


def test_setup_helper_cannot_register_unowned_postgres(guarded):
    factory, _, _ = guarded
    from testing_coverage import owned_postgres_schema

    with pytest.raises(ValueError, match="Fixture-owned"):
        with owned_postgres_schema(
            factory.kw["bind"], object(), "guard_test_" + "a" * 32
        ):
            pytest.fail("Unowned database accepted")


def test_test_setup_keeps_history_append_only(guarded):
    factory, rule_id, _ = guarded
    from testing_coverage import historical_rules

    with factory() as db:
        db.begin()
        historical_rules(
            db,
            DiagnosisServiceRule(
                id=90, diagnosis_id=7, service_id=8, is_covered=True, is_deleted=True
            ),
        )
        history = db.scalar(
            sa.select(CoverageRuleHistory).where(CoverageRuleHistory.rule_id == rule_id)
        )
        history.reason = "Forbidden change"
        with pytest.raises(ValueError, match="append-only"):
            db.flush()
        db.rollback()


@pytest.mark.parametrize("change", ["attribute", "delete"])
def test_late_mapper_change_is_rejected(guarded, change):
    factory, rule_id, _ = guarded

    # Simulate another before_flush listener changing the rule after the first
    # validation. Mapper validation runs before physical SQL.
    def late(db, ctx, instances):
        if db in guards._permits:
            if change == "delete":
                db.delete(guards._permits[db].rule)
            else:
                guards._permits[db].rule.insurer_id = 9

    sa.event.listen(Session, "before_flush", late)
    try:
        with factory() as db:
            db.begin()
            with pytest.raises(CoverageMutationError):
                mutations.update_rule_coverage(db, rule_id, True, context=context())
            db.rollback()
    finally:
        sa.event.remove(Session, "before_flush", late)
    assert counts(factory) == (2, 2)


def test_async_session_uses_orm_guards(tmp_path):
    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from models import Base

    async def verify():
        path = tmp_path / "guard_async.sqlite"
        engine = create_async_engine("sqlite+aiosqlite:///" + path.as_posix())
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory.begin() as db:
                db.add_all(
                    [
                        DiagnosisCode(id=1, code="ASYNC", description="Synthetic"),
                        ServiceCode(id=1, code="ASYNC", description="Synthetic"),
                    ]
                )
            async with factory() as db:
                db.add(DiagnosisServiceRule(diagnosis_id=1, service_id=1))
                with pytest.raises(CoverageMutationError):
                    await db.flush()
                await db.rollback()
            async with factory.begin() as db:
                row = await db.run_sync(
                    lambda sync: mutations.create_rule(
                        sync, 1, 1, True, context=context()
                    )
                )
                await db.run_sync(
                    lambda sync: mutations.update_rule_coverage(
                        sync, row.id, False, context=context()
                    )
                )
            async with factory() as db:
                with pytest.raises(CoverageMutationError):
                    await db.execute(
                        sa.update(DiagnosisServiceRule).values(is_covered=True)
                    )
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(verify())


def test_setup_rejects_persistent_sqlite(tmp_path):
    from testing_coverage import owned_sqlite

    engine = sa.create_engine("sqlite:///" + (tmp_path / "unowned.sqlite").as_posix())
    try:
        with pytest.raises(ValueError, match="in-memory"):
            with owned_sqlite(engine):
                pytest.fail("Unowned persistent file accepted")
    finally:
        engine.dispose()


def test_session_rejects_rule_dml_inside_cte(guarded):
    factory, _, _ = guarded
    statement = sa.select(sa.literal(1)).add_cte(
        sa.insert(DiagnosisServiceRule)
        .values(diagnosis_id=7, service_id=8)
        .returning(DiagnosisServiceRule.id)
        .cte()
    )
    with factory() as db:
        with pytest.raises(CoverageMutationError):
            db.execute(statement)
        db.rollback()


@pytest.mark.parametrize("routed_model", [DiagnosisServiceRule, CoverageRuleHistory])
def test_permit_cannot_cross_mapper_connections(guarded, routed_model):
    factory, _, _ = guarded
    from models import Base

    other = sa.create_engine("sqlite://")
    Base.metadata.create_all(other)
    try:
        with Session(bind=factory.kw["bind"], binds={routed_model: other}) as db:
            db.begin()
            with pytest.raises(CoverageMutationError):
                mutations.create_rule(db, 7, 18, True, context=context())
            db.rollback()
        with other.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(
                        DiagnosisServiceRule.__table__
                    )
                )
                == 0
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(
                        CoverageRuleHistory.__table__
                    )
                )
                == 0
            )
    finally:
        other.dispose()
    assert counts(factory) == (2, 2)


def test_historical_setup_rejects_changed_connection_target(guarded, tmp_path):
    factory, _, _ = guarded
    from testing_coverage import historical_rules

    with factory() as db:
        db.begin()
        connection = db.connection()
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql(
                "ATTACH DATABASE ? AS unowned",
                (str(tmp_path / "unowned_attached.sqlite"),),
            )
            message = "unowned SQLite"
        else:
            connection.exec_driver_sql("SET LOCAL search_path TO public")
            message = "owned schema"
        with pytest.raises(ValueError, match=message):
            historical_rules(
                db, DiagnosisServiceRule(id=101, diagnosis_id=7, service_id=8)
            )
        db.rollback()


@pytest.mark.parametrize("kind", ["update", "delete"])
def test_session_alias_dml_rejected(guarded, kind):
    factory, _, _ = guarded
    alias = DiagnosisServiceRule.__table__.alias("target_rule")
    statement = getattr(sa, kind)(alias)
    if kind == "update":
        statement = statement.values(is_covered=True)
    with factory() as db:
        with pytest.raises(CoverageMutationError):
            db.execute(statement)
        db.rollback()
