"""Reduced application boundary; only disposable destination databases."""

import asyncio

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from diagnosis_systems import ICD10_AM_SYSTEM
from models import Base, DiagnosisServiceRule, DiagnosisCode, ServiceCode
from services import coverage_write_guards as guards
from services import coverage_mutations as mutations
from services.coverage_mutations import CoverageMutationError
from test_coverage_write_guards import guarded, context, counts
from test_coverage_writers import writer_db
from test_terminology_identity import postgres_engine


@pytest.mark.parametrize("kind", ["insert", "update", "delete"])
@pytest.mark.parametrize(
    "target", ["table", "reflected", "alias", "schema", "compiled"]
)
def test_engine_core_rejected(guarded, kind, target):
    factory, _, _ = guarded
    engine = factory.kw["bind"]
    table = DiagnosisServiceRule.__table__
    if target == "reflected":
        table = sa.Table(table.name, sa.MetaData(), autoload_with=engine)
    elif target == "schema":
        with engine.connect() as connection:
            schema = (
                "main"
                if engine.dialect.name == "sqlite"
                else connection.scalar(sa.text("SELECT current_schema()"))
            )
        table = sa.Table(table.name, sa.MetaData(), schema=schema, autoload_with=engine)
    elif target == "alias":
        table = table.alias("rule_target")
    statement = getattr(sa, kind)(table)
    if kind == "insert":
        statement = statement.values(diagnosis_id=7, service_id=8, is_covered=True)
    elif kind == "update":
        statement = statement.values(is_covered=True)
    if target == "compiled":
        statement = statement.compile(dialect=engine.dialect)
    before = counts(factory)
    with engine.begin() as connection:
        with pytest.raises(CoverageMutationError):
            connection.execute(statement)
    assert counts(factory) == before


@pytest.mark.parametrize("kind", ["insert", "update", "delete"])
def test_engine_writable_cte_rejected(guarded, kind):
    factory, _, _ = guarded
    table = DiagnosisServiceRule.__table__
    write = getattr(sa, kind)(table)
    if kind == "insert":
        write = write.values(diagnosis_id=7, service_id=8, is_covered=True)
    elif kind == "update":
        write = write.values(is_covered=True)
    statement = sa.select(sa.literal(1)).add_cte(write.returning(table.c.id).cte())
    with factory.kw["bind"].begin() as connection:
        with pytest.raises(CoverageMutationError):
            connection.execute(statement)


@pytest.mark.parametrize("size", [1, 2])
@pytest.mark.parametrize(
    "method",
    [
        "save_insert",
        "save_insert_defaults",
        "save_update",
        "save_update_defaults",
        "insert",
        "insert_defaults",
        "update",
    ],
)
def test_engine_legacy_bulk_rejected(guarded, size, method):
    factory, rule_id, other_id = guarded
    before = counts(factory)
    with factory() as db:
        if "update" in method:
            rows = [
                {"id": row_id, "is_covered": True}
                for row_id in (rule_id, other_id)[:size]
            ]
        else:
            rows = [
                {"id": 90 + i, "diagnosis_id": 7, "service_id": 8, "is_covered": True}
                for i in range(size)
            ]
        with pytest.raises(CoverageMutationError):
            if method.startswith("save"):
                if "update" in method:
                    objects = [db.get(DiagnosisServiceRule, row["id"]) for row in rows]
                    for obj in objects:
                        db.expunge(obj)
                        obj.is_covered = True
                else:
                    objects = [DiagnosisServiceRule(**row) for row in rows]
                db.bulk_save_objects(
                    objects, return_defaults=method.endswith("defaults")
                )
            elif method.startswith("insert"):
                db.bulk_insert_mappings(
                    DiagnosisServiceRule,
                    rows,
                    return_defaults=method.endswith("defaults"),
                )
            else:
                db.bulk_update_mappings(DiagnosisServiceRule, rows)
        db.rollback()
    assert counts(factory) == before


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO diagnosis_service_rules (diagnosis_id, service_id) VALUES (7,8)",
        "UPDATE diagnosis_service_rules SET is_covered = true",
        "DELETE FROM diagnosis_service_rules",
        "SELECT 1",
        "SET TRANSACTION READ ONLY",
    ],
)
def test_session_unregistered_text_rejected(guarded, sql):
    factory, _, _ = guarded
    with factory() as db:
        with pytest.raises(CoverageMutationError):
            db.execute(sa.text(sql))
        db.rollback()


def test_session_embedded_text_rejected(guarded):
    factory, _, _ = guarded
    with factory() as db:
        with pytest.raises(CoverageMutationError):
            db.execute(
                sa.select(DiagnosisServiceRule).from_statement(
                    sa.text("SELECT * FROM diagnosis_service_rules")
                )
            )
        db.rollback()


@pytest.mark.parametrize(
    "template,parameters",
    [
        ("unknown", {}),
        ("coverage_lock", {"extra": 1}),
        ("read_only", {"extra": 1}),
        ("icd_advisory", {"system": "other"}),
        ("icd_advisory", {}),
    ],
)
def test_session_template_parameter_mismatch(guarded, template, parameters):
    factory, _, _ = guarded
    with factory.begin() as db:
        with pytest.raises(CoverageMutationError):
            guards._execute_session_text(db, template, parameters)
        assert db not in guards._text_executions


@pytest.mark.parametrize("writer_db", ["postgresql"], indirect=True)
def test_session_templates_are_bounded(writer_db):
    captured = []

    def observe(state):
        if isinstance(state.statement, sa.sql.elements.TextClause):
            captured.append((state.statement, state.parameters))
            with pytest.raises(CoverageMutationError):
                state.session.execute(state.statement, state.parameters)

    sa.event.listen(Session, "do_orm_execute", observe)
    try:
        with writer_db.begin() as db:
            guards._execute_session_text(db, "coverage_lock")
            guards._execute_session_text(
                db, "icd_advisory", {"system": ICD10_AM_SYSTEM}
            )
            assert db not in guards._text_executions
        with writer_db.begin() as db:
            guards._execute_session_text(db, "read_only")
            assert db.scalar(sa.select(sa.literal(1))) == 1
            statement, parameters = captured[-1]
            with pytest.raises(CoverageMutationError):
                db.execute(statement, parameters)
    finally:
        sa.event.remove(Session, "do_orm_execute", observe)
    assert len(captured) == 3


@pytest.mark.parametrize("mismatch", ["row", "payload", "operation", "batch", "shape"])
def test_mapper_execution_mismatch_rejected(guarded, mismatch):
    factory, rule_id, other_id = guarded
    table = DiagnosisServiceRule.__table__
    attempted = []

    def before(mapper, connection, rule):
        execution = guards._executions[connection]
        payload = dict(execution.payload)
        statement = table.update().where(
            table.c.id == sa.bindparam("diagnosis_service_rules_id")
        )
        if mismatch == "row":
            payload["diagnosis_service_rules_id"] = other_id
        elif mismatch == "payload":
            payload["is_covered"] = False
        elif mismatch == "operation":
            statement = table.insert()
        elif mismatch == "batch":
            payload = [payload, payload]
        else:
            statement = sa.select(sa.literal(1)).add_cte(
                statement.returning(table.c.id).cte()
            )
        with pytest.raises(CoverageMutationError):
            connection.execute(statement, payload)
        assert not execution.consumed
        attempted.append(True)

    sa.event.listen(DiagnosisServiceRule, "before_update", before)
    try:
        with factory.begin() as db:
            mutations.update_rule_coverage(db, rule_id, True, context=context())
            assert db.connection() not in guards._executions
    finally:
        sa.event.remove(DiagnosisServiceRule, "before_update", before)
    assert attempted == [True]
    assert counts(factory) == (2, 3)


def test_mapper_execution_cleanup_on_failure_and_savepoint(guarded):
    factory, rule_id, _ = guarded
    captured = []

    def fail(mapper, connection, rule):
        captured.append((connection, guards._executions[connection]))
        raise RuntimeError("Synthetic mapper failure")

    sa.event.listen(DiagnosisServiceRule, "before_update", fail)
    try:
        with factory() as db:
            db.begin()
            db.begin_nested()
            with pytest.raises(RuntimeError, match="mapper failure"):
                mutations.update_rule_coverage(db, rule_id, True, context=context())
            assert captured[0][0] not in guards._executions
            db.rollback()
    finally:
        sa.event.remove(DiagnosisServiceRule, "before_update", fail)
    with factory.begin() as db:
        with db.begin_nested():
            mutations.update_rule_coverage(db, rule_id, True, context=context())
        connection = db.connection()
        assert connection not in guards._executions
        stale = captured[0][1]
        guards._executions[connection] = stale
        try:
            with pytest.raises(CoverageMutationError):
                connection.execute(
                    DiagnosisServiceRule.__table__.update().where(
                        DiagnosisServiceRule.id
                        == sa.bindparam("diagnosis_service_rules_id")
                    ),
                    stale.payload,
                )
        finally:
            guards._executions.pop(connection, None)
    assert counts(factory) == (2, 3)


@pytest.mark.parametrize("size", [1, 2])
def test_mapper_rejects_inline_multi_values(guarded, size):
    factory, _, _ = guarded
    attempted = []

    def before(mapper, connection, rule):
        execution = guards._executions[connection]
        payload = execution.payload
        statement = DiagnosisServiceRule.__table__.insert().values(
            [dict(payload)] * size
        )
        with pytest.raises(CoverageMutationError):
            connection.execute(statement, payload)
        assert not execution.consumed
        attempted.append(True)

    sa.event.listen(DiagnosisServiceRule, "before_insert", before)
    try:
        with factory.begin() as db:
            mutations.create_rule(db, 7, 8, True, context=context())
    finally:
        sa.event.remove(DiagnosisServiceRule, "before_insert", before)
    assert attempted == [True]
    assert counts(factory) == (3, 3)


def test_execution_is_bound_to_rule_object_and_consumed_once(guarded):
    factory, rule_id, _ = guarded
    engine = factory.kw["bind"]
    captured = []

    def before(mapper, connection, rule):
        execution = guards._executions[connection]
        permit = execution.permit
        original = permit.rule
        permit.rule = DiagnosisServiceRule(**permit.after)
        try:
            with pytest.raises(CoverageMutationError):
                execution.check(connection)
        finally:
            permit.rule = original

    def after(connection, statement, multi, params, options, result):
        if getattr(statement, "table", None) is DiagnosisServiceRule.__table__:
            execution = guards._executions[connection]
            assert execution.consumed
            with pytest.raises(CoverageMutationError):
                connection.execute(statement, params if params else multi)
            captured.append(execution)

    sa.event.listen(DiagnosisServiceRule, "before_update", before)
    sa.event.listen(engine, "after_execute", after)
    try:
        with factory.begin() as db:
            mutations.update_rule_coverage(db, rule_id, True, context=context())
            connection = db.connection()
            assert connection not in guards._executions
            guards._executions[connection] = captured[0]
            try:
                with pytest.raises(CoverageMutationError):
                    captured[0].check(connection)
            finally:
                guards._executions.pop(connection, None)
    finally:
        sa.event.remove(DiagnosisServiceRule, "before_update", before)
        sa.event.remove(engine, "after_execute", after)
    assert counts(factory) == (2, 3)


def test_historical_execution_exact_statement_payload_and_connection(guarded):
    from testing_coverage import _verify_connection

    factory, _, _ = guarded
    engine = factory.kw["bind"]
    table = DiagnosisServiceRule.__table__
    statement = table.insert()
    payload = {"id": 91, "diagnosis_id": 7, "service_id": 8, "is_covered": True}
    other_engine = sa.create_engine(URL.create("sqlite"))
    guards.register_coverage_engine(other_engine)
    try:
        with engine.begin() as connection, other_engine.begin() as other:
            _verify_connection(connection)
            with guards._historical_execution(connection, statement, payload):
                execution = guards._executions[connection]
                with pytest.raises(CoverageMutationError):
                    connection.execute(table.insert(), payload)
                with pytest.raises(CoverageMutationError):
                    connection.execute(statement, {**payload, "id": 92})
                with pytest.raises(CoverageMutationError):
                    connection.execute(statement, [payload, payload])
                guards._executions[other] = execution
                try:
                    with pytest.raises(CoverageMutationError):
                        other.execute(statement, payload)
                finally:
                    guards._executions.pop(other, None)
                connection.execute(statement, payload)
                with pytest.raises(CoverageMutationError):
                    connection.execute(statement, payload)
            assert connection not in guards._executions
    finally:
        other_engine.dispose()
    assert counts(factory) == (3, 2)


def test_historical_execution_rollback_clears_state(guarded):
    from testing_coverage import _verify_connection

    factory, _, _ = guarded
    with factory.kw["bind"].connect() as connection:
        _verify_connection(connection)
        statement = DiagnosisServiceRule.__table__.insert()
        payload = {"id": 91, "diagnosis_id": 7, "service_id": 8}
        with pytest.raises(CoverageMutationError):
            with guards._historical_execution(connection, statement, payload):
                execution = guards._executions[connection]
                connection.rollback()
                assert connection not in guards._executions
                connection.begin()
                guards._executions[connection] = execution
                connection.execute(statement, payload)
        assert connection not in guards._executions
        connection.rollback()
    assert counts(factory) == (2, 2)


def test_connection_raw_sql_remains_an_explicit_exclusion(guarded):
    factory, _, _ = guarded
    with factory.kw["bind"].connect() as connection:
        assert connection.scalar(sa.text("SELECT 1")) == 1
        assert connection.exec_driver_sql("SELECT 1").scalar() == 1


def test_unrelated_core_and_select_sanity(guarded, monkeypatch):
    factory, _, _ = guarded
    engine = factory.kw["bind"]
    with engine.begin() as connection:
        result = connection.execute(
            DiagnosisCode.__table__.insert(),
            {"code": "CORE-SAFE", "description": "Synthetic"},
        )
        row_id = result.inserted_primary_key[0]
        connection.execute(
            DiagnosisCode.__table__.update().where(DiagnosisCode.id == row_id),
            {"description": "Updated"},
        )
        connection.execute(
            DiagnosisCode.__table__.delete().where(DiagnosisCode.id == row_id)
        )
    queries = []

    def observe(*args):
        queries.append(True)

    def unexpected(*args):
        pytest.fail("Read path acquired a write payload or object snapshot")

    sa.event.listen(engine, "before_cursor_execute", observe)
    try:
        monkeypatch.setattr(guards, "_same_payload", unexpected)
        monkeypatch.setattr(guards, "_snapshot", unexpected)
        with factory() as db:
            for _ in range(30):
                assert len(db.scalars(sa.select(DiagnosisServiceRule)).all()) == 2
    finally:
        sa.event.remove(engine, "before_cursor_execute", observe)
    assert len(queries) == 30


def test_project_registration_is_automatic_and_idempotent():
    import database
    import async_database

    for engine in (
        database.SessionLocal.kw["bind"],
        async_database.async_engine.sync_engine,
    ):
        assert sa.event.contains(engine, "before_execute", guards._engine_execute)
        guards.register_coverage_engine(engine)
        guards.register_coverage_engine(engine)
        assert sa.event.contains(engine, "before_execute", guards._engine_execute)


def test_async_engine_and_audited_services(writer_db):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    sync_engine = writer_db.kw["bind"]

    async def verify():
        if sync_engine.dialect.name == "sqlite":
            engine = create_async_engine(URL.create("sqlite+aiosqlite"))
        else:
            from testing_coverage import _owners

            schema = _owners[sync_engine][1]
            engine = create_async_engine(
                sync_engine.url.set(drivername="postgresql+psycopg"),
                connect_args={"options": f"-csearch_path={schema}"},
            )
        guards.register_coverage_engine(engine.sync_engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory.begin() as db:
                db.add_all(
                    [
                        DiagnosisCode(id=7, code="ASYNC", description="Synthetic"),
                        ServiceCode(id=8, code="ASYNC", description="Synthetic"),
                    ]
                )
            async with factory.begin() as db:
                row = await db.run_sync(
                    lambda sync: mutations.create_rule(
                        sync, 7, 8, True, context=context()
                    )
                )
                await db.run_sync(
                    lambda sync: mutations.update_rule_coverage(
                        sync, row.id, False, context=context()
                    )
                )
                await db.run_sync(
                    lambda sync: mutations.soft_delete_rule(
                        sync, row.id, context=context()
                    )
                )
                await db.run_sync(
                    lambda sync: mutations.restore_rule(sync, row.id, context=context())
                )
                await db.run_sync(
                    lambda sync: mutations.import_rule(
                        sync, 99, 7, 8, True, context=context()
                    )
                )
            for kind in ("insert", "update", "delete"):
                statement = getattr(sa, kind)(DiagnosisServiceRule.__table__)
                if kind == "insert":
                    statement = statement.values(diagnosis_id=7, service_id=8)
                elif kind == "update":
                    statement = statement.values(is_covered=True)
                async with engine.begin() as connection:
                    with pytest.raises(CoverageMutationError):
                        await connection.execute(statement)
                    with pytest.raises(CoverageMutationError):
                        await connection.run_sync(lambda sync: sync.execute(statement))
            async with factory() as db:
                assert (
                    await db.scalar(
                        sa.select(sa.func.count()).select_from(DiagnosisServiceRule)
                    )
                    == 2
                )
        finally:
            await engine.dispose()

    # Psycopg async needs a selector loop on Windows; Runner is available on 3.11+.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(verify())
