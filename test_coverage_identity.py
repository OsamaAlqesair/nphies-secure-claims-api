"""Current identity invariants on migrated SQLite and disposable PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import json
import sqlite3
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from models import DiagnosisServiceRule, CoverageRuleHistory, InsuranceCompany
from services import coverage_mutations as mutations
from testing_coverage import historical_row
from test_coverage_writers import writer_db, mappings, config, snapshot, legacy_source
from test_terminology_identity import postgres_engine

HEAD = "0009_coverage_rule_current_identity"
PARENT = "0008_coverage_writer_sources"
GLOBAL = "uq_diagnosis_service_rule_current_global"
INSURER = "uq_diagnosis_service_rule_current_insurer"


def context():
    return mutations.MutationContext(reason="Synthetic identity verification")


def insert(connection, row_id, scope=None, deleted=False):
    return historical_row(
        connection,
        id=row_id,
        diagnosis_id=7,
        service_id=8,
        insurer_id=scope,
        is_covered=True,
        is_deleted=deleted,
    )


def indexes(factory):
    return {
        i["name"]: i
        for i in sa.inspect(factory.kw["bind"]).get_indexes("diagnosis_service_rules")
        if i["name"] in (GLOBAL, INSURER)
    }


def version(factory):
    with factory.kw["bind"].connect() as connection:
        return connection.scalar(sa.text("SELECT version_num FROM alembic_version"))


@pytest.mark.parametrize("scope", [None, 9])
def test_database_rejects_current_duplicate(writer_db, scope):
    mappings(writer_db)
    engine = writer_db.kw["bind"]
    with engine.begin() as connection:
        insert(connection, 10, scope)
    before = snapshot(writer_db)
    with pytest.raises(sa.exc.IntegrityError) as error:
        with engine.begin() as connection:
            insert(connection, 11, scope)
    assert mutations._is_identity_violation(error.value)
    assert snapshot(writer_db) == before
    if engine.dialect.name == "postgresql":
        assert error.value.orig.sqlstate == "23505"
        assert error.value.orig.diag.constraint_name == (
            GLOBAL if scope is None else INSURER
        )


def test_database_allows_separate_scopes_and_deleted_peers(writer_db):
    mappings(writer_db)
    with writer_db.begin() as db:
        db.add(InsuranceCompany(id=19, name="Other synthetic insurer"))
    with writer_db.kw["bind"].begin() as connection:
        for row_id, scope in enumerate((None, 9, 19), 10):
            insert(connection, row_id, scope)
        for row_id in range(20, 35):
            insert(connection, row_id, (None, 9, 19)[row_id % 3], True)
    assert len(snapshot(writer_db)["diagnosis_service_rules"]) == 18


@pytest.mark.parametrize("scope", [None, 9])
@pytest.mark.parametrize("operation", ["create", "import"])
def test_service_conflict_preserves_rules_and_history(writer_db, scope, operation):
    mappings(writer_db)
    with writer_db.begin() as db:
        first = mutations.create_rule(
            db, 7, 8, False, insurer_id=scope, context=context()
        )
    before = snapshot(writer_db)
    with pytest.raises(mutations.CoverageMutationError) as error:
        with writer_db.begin() as db:
            if operation == "create":
                mutations.create_rule(
                    db, 7, 8, True, insurer_id=scope, context=context()
                )
            else:
                mutations.import_rule(
                    db, 100, 7, 8, True, insurer_id=scope, context=context()
                )
    assert error.value.code == f"rule_{operation}_conflict"
    assert snapshot(writer_db) == before
    assert before["coverage_rule_history"][0].rule_id == first.id


@pytest.mark.parametrize("scope", [None, 9])
def test_soft_delete_recreate_restore_exact_history(writer_db, scope):
    mappings(writer_db)
    with writer_db.begin() as db:
        a = mutations.create_rule(db, 7, 8, False, insurer_id=scope, context=context())
        mutations.soft_delete_rule(db, a.id, context=context())
        b = mutations.create_rule(db, 7, 8, True, insurer_id=scope, context=context())
        mutations.import_rule(
            db, 100, 7, 8, False, insurer_id=scope, is_deleted=True, context=context()
        )
    before = snapshot(writer_db)
    with pytest.raises(mutations.CoverageMutationError) as error:
        with writer_db.begin() as db:
            mutations.restore_rule(db, a.id, context=context())
    assert error.value.code == "rule_restore_conflict"
    assert snapshot(writer_db) == before
    with writer_db.begin() as db:
        mutations.soft_delete_rule(db, b.id, context=context())
        mutations.restore_rule(db, a.id, context=context())
    state = snapshot(writer_db)
    assert [
        (r.id, r.is_deleted, r.is_covered) for r in state["diagnosis_service_rules"]
    ] == [(a.id, False, False), (b.id, True, True), (100, True, False)]
    assert [(h.rule_id, h.action) for h in state["coverage_rule_history"]] == [
        (a.id, "CREATE"),
        (a.id, "SOFT_DELETE"),
        (b.id, "CREATE"),
        (100, "CREATE"),
        (b.id, "SOFT_DELETE"),
        (a.id, "RESTORE"),
    ]


def test_deleted_mapping_does_not_release_current_identity(writer_db):
    mappings(writer_db)
    with writer_db.kw["bind"].begin() as connection:
        insert(connection, 10, 9)
        connection.execute(
            InsuranceCompany.__table__.update()
            .where(InsuranceCompany.id == 9)
            .values(is_deleted=True)
        )
    with pytest.raises(sa.exc.IntegrityError):
        with writer_db.kw["bind"].begin() as connection:
            insert(connection, 11, 9)


@pytest.mark.parametrize("scope", [None, 9])
@pytest.mark.parametrize("operation", ["create", "import", "restore"])
def test_database_authority_and_late_conflict_rolls_back(
    writer_db, monkeypatch, caplog, scope, operation
):
    mappings(writer_db)
    with writer_db.kw["bind"].begin() as connection:
        insert(connection, 10, scope)
        insert(connection, 11, scope, True)
    before = snapshot(writer_db)
    # Make the service's precheck miss an existing peer. The real DB must reject
    # the mutation, rather than permitting test-only historical authorization.
    original = sa.orm.Session.scalar

    def miss_peer(db, statement, *args, **kwargs):
        if isinstance(statement, sa.sql.Select) and tuple(
            statement.selected_columns
        ) == (DiagnosisServiceRule.__table__.c.id,):
            return None
        return original(db, statement, *args, **kwargs)

    monkeypatch.setattr(sa.orm.Session, "scalar", miss_peer)
    with pytest.raises(mutations.CoverageMutationError) as error:
        with writer_db.begin() as db:
            if operation == "create":
                mutations.create_rule(
                    db, 7, 8, False, insurer_id=scope, context=context()
                )
            elif operation == "import":
                mutations.import_rule(
                    db, 12, 7, 8, False, insurer_id=scope, context=context()
                )
            else:
                mutations.restore_rule(db, 11, context=context())
    assert error.value.code == f"rule_{operation}_conflict"
    assert error.value.__suppress_context__
    assert str(error.value) == "Another current rule has the same identity."
    assert snapshot(writer_db) == before
    assert not caplog.records


@pytest.mark.parametrize(
    "state,name,expected",
    [
        ("23505", GLOBAL, True),
        ("23505", INSURER, True),
        ("23505", "unrelated_unique", False),
        ("23503", GLOBAL, False),
        ("23514", GLOBAL, False),
        (None, GLOBAL, False),
        ("23505", None, False),
    ],
)
def test_postgres_classifier_requires_both_diagnostics(state, name, expected):
    original = SimpleNamespace(
        sqlstate=state, diag=SimpleNamespace(constraint_name=name)
    )
    error = sa.exc.IntegrityError("PRIVATE_SQL", {}, original)
    assert mutations._is_identity_violation(error) is expected
    for action in ("create", "restore", "import"):
        assert mutations._lifecycle_failure(action, error).code == (
            f"rule_{action}_conflict" if expected else "rule_mutation_failed"
        )


@pytest.mark.parametrize(
    "message,code,expected",
    [
        (
            "UNIQUE constraint failed: diagnosis_service_rules.diagnosis_id, diagnosis_service_rules.service_id",
            2067,
            True,
        ),
        (
            "UNIQUE constraint failed: diagnosis_service_rules.diagnosis_id, diagnosis_service_rules.service_id, diagnosis_service_rules.insurer_id",
            2067,
            True,
        ),
        ("UNIQUE constraint failed: diagnosis_service_rules.id", 1555, False),
        ("UNIQUE constraint failed: unrelated.id", 2067, False),
        ("FOREIGN KEY constraint failed", 787, False),
        ("CHECK constraint failed: ck_rule_history_transition", 275, False),
        ("unknown", 2067, False),
        (
            "UNIQUE constraint failed: diagnosis_service_rules.diagnosis_id, diagnosis_service_rules.service_id",
            19,
            False,
        ),
    ],
)
def test_sqlite_classifier_is_narrow(message, code, expected):
    original = sqlite3.IntegrityError(message)
    original.sqlite_errorcode = code
    assert (
        mutations._is_identity_violation(
            sa.exc.IntegrityError("PRIVATE_SQL", {}, original)
        )
        is expected
    )


@pytest.mark.parametrize("scope", [None, 9])
@pytest.mark.parametrize(
    "case", ["current_and_deleted", "deleted_only", "current_conflict"]
)
def test_legacy_source_identity_policy_before_destination_writes(
    writer_db, tmp_path, scope, case
):
    from migrate_sqlite import import_legacy

    source = legacy_source(tmp_path, timestamps=True)
    with sqlite3.connect(source) as legacy:
        legacy.execute("DELETE FROM diagnosis_service_rules")
        for row_id, deleted in [
            (10, case == "deleted_only"),
            (11, case != "current_conflict"),
            (12, True),
        ]:
            legacy.execute(
                "INSERT INTO diagnosis_service_rules VALUES (?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    7,
                    8,
                    scope,
                    True,
                    deleted,
                    "2000-01-02T00:00:00+00:00",
                    "2001-01-02T00:00:00+00:00",
                ),
            )
    original_bytes = source.read_bytes()
    engine = writer_db.kw["bind"]
    writes = []

    def observe(connection, cursor, statement, parameters, ctx, executemany):
        if (
            statement.lstrip()
            .upper()
            .startswith(("INSERT", "UPDATE", "DELETE", "SELECT SETVAL"))
        ):
            writes.append(True)

    sa.event.listen(engine, "before_cursor_execute", observe)
    try:
        if case == "current_conflict":
            before = snapshot(writer_db)
            with pytest.raises(mutations.CoverageMutationError) as error:
                import_legacy(source, engine)
            assert error.value.code == "rule_import_conflict"
            assert not writes
            assert snapshot(writer_db) == before
        else:
            import_legacy(source, engine)
            state = snapshot(writer_db)
            assert [r.id for r in state["diagnosis_service_rules"]] == [10, 11, 12]
            assert [r.is_deleted for r in state["diagnosis_service_rules"]] == [
                case == "deleted_only",
                True,
                True,
            ]
            assert len(state["coverage_rule_history"]) == 3
            assert all(h.action == "CREATE" for h in state["coverage_rule_history"])
            assert all(
                r.created_at.year == 2000 and r.updated_at.year == 2001
                for r in state["diagnosis_service_rules"]
            )
    finally:
        sa.event.remove(engine, "before_cursor_execute", observe)
    assert source.read_bytes() == original_bytes


def test_legacy_preflight_uses_explicit_checked_snapshot(
    writer_db, tmp_path, monkeypatch
):
    import migrate_sqlite

    source = legacy_source(tmp_path)
    with sqlite3.connect(source) as legacy:
        legacy.execute("PRAGMA journal_mode=WAL")
    original = migrate_sqlite._import_destination
    original_snapshot = migrate_sqlite._legacy_snapshot

    def read(legacy):
        assert legacy.in_transaction
        return original_snapshot(legacy)

    def verify(snapshot_rows, target, connection):
        # Snapshot is already materialized; destination insertion never re-reads.
        with sqlite3.connect(source) as legacy:
            assert (
                legacy.execute(
                    "SELECT count(*) FROM diagnosis_service_rules"
                ).fetchone()[0]
                == 4
            )
            legacy.execute(
                "INSERT INTO diagnosis_service_rules VALUES (99,7,8,NULL,1,0)"
            )
        assert [r["id"] for r in snapshot_rows["diagnosis_service_rules"]] == [
            10,
            11,
            12,
            13,
        ]
        return original(snapshot_rows, target, connection)

    monkeypatch.setattr(migrate_sqlite, "_import_destination", verify)
    monkeypatch.setattr(migrate_sqlite, "_legacy_snapshot", read)
    migrate_sqlite.import_legacy(source, writer_db.kw["bind"])
    assert len(snapshot(writer_db)["diagnosis_service_rules"]) == 4


def test_late_legacy_database_conflict_rolls_back_every_destination_table(
    writer_db, tmp_path, monkeypatch
):
    import migrate_sqlite

    source = legacy_source(tmp_path)
    original = migrate_sqlite.import_rule
    calls = []

    def race(db, rule_id, *args, **kwargs):
        calls.append(rule_id)
        if len(calls) == 2:
            insert(db.connection(), 200, 9)
        return original(db, rule_id, *args, **kwargs)

    monkeypatch.setattr(migrate_sqlite, "import_rule", race)
    monkeypatch.setattr(mutations, "_current_peer", lambda *args: None)
    before = snapshot(writer_db)
    with pytest.raises(mutations.CoverageMutationError) as error:
        migrate_sqlite.import_legacy(source, writer_db.kw["bind"])
    assert calls == [10, 11] and error.value.code == "rule_import_conflict"
    assert snapshot(writer_db) == before


@pytest.mark.parametrize("failure", ["primary_key", "foreign_key"])
def test_real_unrelated_database_constraints_stay_generic(writer_db, failure):
    mappings(writer_db)
    engine = writer_db.kw["bind"]
    with engine.begin() as connection:
        insert(connection, 10, deleted=True)
    with pytest.raises(sa.exc.IntegrityError) as error:
        with engine.begin() as connection:
            if failure == "primary_key":
                insert(connection, 10, deleted=True)
            else:
                insert(connection, 11, 999)
    assert not mutations._is_identity_violation(error.value)
    assert (
        mutations._lifecycle_failure("import", error.value).code
        == "rule_mutation_failed"
    )


@pytest.mark.parametrize("scope", [None, 9, "both"])
def test_migration_all_conflicts_preserve_data_and_revision(writer_db, scope):
    mappings(writer_db)
    first_scope = None if scope == "both" else scope
    with writer_db.begin() as db:
        mutations.import_rule(
            db, 10, 7, 8, True, insurer_id=first_scope, context=context()
        )
    command.downgrade(config(), PARENT)
    scopes = [None, 9] if scope == "both" else [scope]
    with writer_db.kw["bind"].begin() as connection:
        for n, insurer in enumerate(scopes):
            if n:
                insert(connection, 10 + n * 2, insurer)
            insert(connection, 11 + n * 2, insurer)
    before = snapshot(writer_db)
    with pytest.raises(
        RuntimeError, match="Current coverage rule conflicts: "
    ) as error:
        command.upgrade(config(), HEAD)
    details = json.loads(str(error.value).split(": ", 1)[1])
    assert details == [
        dict(
            diagnosis_id=7,
            service_id=8,
            scope="GLOBAL" if s is None else s,
            current_count=2,
        )
        for s in scopes
    ]
    assert snapshot(writer_db) == before
    assert version(writer_db) == PARENT
    assert indexes(writer_db) == {}


def test_migration_clean_indexes_lossless_downgrade_and_reupgrade(writer_db):
    mappings(writer_db)
    with writer_db.begin() as db:
        mutations.create_rule(db, 7, 8, False, context=context())
        mutations.import_rule(db, 100, 7, 8, True, is_deleted=True, context=context())
    before = snapshot(writer_db)
    command.downgrade(config(), PARENT)
    assert not indexes(writer_db) and snapshot(writer_db) == before
    command.upgrade(config(), HEAD)
    assert snapshot(writer_db) == before and version(writer_db) == HEAD
    reflected = indexes(writer_db)
    for name, columns, null_test in [
        (GLOBAL, ["diagnosis_id", "service_id"], "insurer_id is null"),
        (
            INSURER,
            ["diagnosis_id", "service_id", "insurer_id"],
            "insurer_id is not null",
        ),
    ]:
        index = reflected[name]
        assert index["unique"] and index["column_names"] == columns
        dialect = writer_db.kw["bind"].dialect.name
        predicate = (
            str(index["dialect_options"][f"{dialect}_where"])
            .lower()
            .replace("(", "")
            .replace(")", "")
        )
        assert null_test in predicate and "is_deleted = false" in predicate
    command.downgrade(config(), PARENT)
    with writer_db.kw["bind"].begin() as connection:
        insert(connection, 101)
    before = snapshot(writer_db)
    with pytest.raises(RuntimeError, match="Current coverage rule conflicts"):
        command.upgrade(config(), HEAD)
    assert (
        snapshot(writer_db) == before
        and version(writer_db) == PARENT
        and not indexes(writer_db)
    )


def test_second_index_failure_rolls_back_first(writer_db):
    command.downgrade(config(), PARENT)
    engine = writer_db.kw["bind"]

    def reject(connection, cursor, statement, parameters, ctx, executemany):
        if statement.startswith("CREATE UNIQUE INDEX " + INSURER):
            raise sa.exc.OperationalError(
                "PRIVATE_SQL", {}, RuntimeError("PRIVATE_DETAIL")
            )

    before = snapshot(writer_db)
    sa.event.listen(engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError, match="identity migration failed"):
            command.upgrade(config(), HEAD)
    finally:
        sa.event.remove(engine, "before_cursor_execute", reject)
    assert (
        indexes(writer_db) == {}
        and version(writer_db) == PARENT
        and snapshot(writer_db) == before
    )


def test_offline_migration_refuses_before_index_ddl():
    output = StringIO()
    cfg = Config("alembic.ini", output_buffer=output)
    with pytest.raises(RuntimeError, match="requires online preflight"):
        command.upgrade(cfg, PARENT + ":" + HEAD, sql=True)
    assert "CREATE UNIQUE INDEX" not in output.getvalue()


@pytest.mark.parametrize("writer_db", ["postgresql"], indirect=True)
@pytest.mark.parametrize("scope", [None, 9])
@pytest.mark.parametrize("race", ["create_create", "create_restore"])
def test_postgres_same_identity_races(writer_db, scope, race):
    mappings(writer_db)
    if race == "create_restore":
        with writer_db.begin() as db:
            deleted = mutations.import_rule(
                db,
                100,
                7,
                8,
                False,
                insurer_id=scope,
                is_deleted=True,
                context=context(),
            )
    barrier = Barrier(2)

    def mutate(n):
        barrier.wait(timeout=10)
        try:
            with writer_db.begin() as db:
                if n == 1 and race == "create_restore":
                    mutations.restore_rule(db, deleted.id, context=context())
                else:
                    mutations.create_rule(
                        db, 7, 8, True, insurer_id=scope, context=context()
                    )
            return "success"
        except mutations.CoverageMutationError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(mutate, [0, 1]))
    assert outcomes.count("success") == 1
    assert set(outcomes) <= {"success", "rule_create_conflict", "rule_restore_conflict"}
    state = snapshot(writer_db)
    assert sum(not r.is_deleted for r in state["diagnosis_service_rules"]) == 1
    events = [
        h
        for h in state["coverage_rule_history"]
        if not (race == "create_restore" and h.rule_id == 100 and h.action == "CREATE")
    ]
    assert len(events) == 1


@pytest.mark.parametrize("writer_db", ["postgresql"], indirect=True)
def test_postgres_migration_blocks_writer_across_preflight_and_index_creation(
    writer_db,
):
    mappings(writer_db)
    command.downgrade(config(), PARENT)
    engine = writer_db.kw["bind"]
    with engine.begin() as connection:
        insert(connection, 10)
    checked, attempted, completed = Event(), Event(), Event()

    def hold(connection, cursor, statement, parameters, ctx, executemany):
        if statement.startswith("CREATE UNIQUE INDEX " + GLOBAL):
            checked.set()
            assert attempted.wait(timeout=10)
            assert not completed.wait(timeout=0.2)

    def write():
        assert checked.wait(timeout=10)
        try:
            with engine.begin() as connection:
                connection.execute(sa.text("SET LOCAL lock_timeout = '8s'"))
                attempted.set()
                insert(connection, 11)
        except sa.exc.IntegrityError as error:
            assert mutations._is_identity_violation(error)
            return "conflict"
        finally:
            completed.set()
        return "unexpected_success"

    sa.event.listen(engine, "before_cursor_execute", hold)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(write)
            command.upgrade(config(), HEAD)
            assert writer.result(timeout=15) == "conflict"
    finally:
        sa.event.remove(engine, "before_cursor_execute", hold)
    assert version(writer_db) == HEAD and len(indexes(writer_db)) == 2
    assert len(snapshot(writer_db)["diagnosis_service_rules"]) == 1


@pytest.mark.parametrize("deleted", [None, 0, 1, "nonblank"])
def test_legacy_preflight_and_import_share_deletion_normalization(
    writer_db, tmp_path, deleted
):
    from migrate_sqlite import import_legacy

    source = legacy_source(tmp_path)
    with sqlite3.connect(source) as legacy:
        legacy.execute(
            "UPDATE diagnosis_service_rules SET is_deleted = ? WHERE id = 12",
            (deleted,),
        )
    if bool(deleted):
        import_legacy(source, writer_db.kw["bind"])
        assert snapshot(writer_db)["diagnosis_service_rules"][2].is_deleted is True
    else:
        before = snapshot(writer_db)
        with pytest.raises(mutations.CoverageMutationError) as error:
            import_legacy(source, writer_db.kw["bind"])
        assert error.value.code == "rule_import_conflict"
        assert snapshot(writer_db) == before


def test_legacy_all_conflicts_have_sorted_controlled_diagnostics(writer_db, tmp_path):
    from migrate_sqlite import import_legacy

    source = legacy_source(tmp_path)
    with sqlite3.connect(source) as legacy:
        legacy.executemany(
            "INSERT INTO diagnosis_service_rules VALUES (?,?,?,?,?,?)",
            [(90, 7, 8, 9, False, False), (91, 7, 8, None, False, False)],
        )
    with pytest.raises(mutations.CoverageMutationError) as error:
        import_legacy(source, writer_db.kw["bind"])
    assert error.value.code == "rule_import_conflict"
    assert json.loads(str(error.value).split(": ", 1)[1]) == [
        dict(diagnosis_id=7, service_id=8, scope="GLOBAL", current_count=2),
        dict(diagnosis_id=7, service_id=8, scope=9, current_count=2),
    ]


@pytest.mark.parametrize("writer_db", ["postgresql"], indirect=True)
def test_postgres_required_revision_capacity_is_transactional(writer_db):
    command.downgrade(config(), PARENT)
    engine = writer_db.kw["bind"]
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE varchar(32)"
        )

    def capacity():
        return next(
            c["type"].length
            for c in sa.inspect(engine).get_columns("alembic_version")
            if c["name"] == "version_num"
        )

    def reject(connection, cursor, statement, parameters, ctx, executemany):
        if statement.startswith("CREATE UNIQUE INDEX " + INSURER):
            raise sa.exc.OperationalError(
                "PRIVATE_SQL", {}, RuntimeError("PRIVATE_DETAIL")
            )

    sa.event.listen(engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError, match="identity migration failed"):
            command.upgrade(config(), HEAD)
    finally:
        sa.event.remove(engine, "before_cursor_execute", reject)
    assert capacity() == 32 and version(writer_db) == PARENT and not indexes(writer_db)
    command.upgrade(config(), HEAD)
    assert capacity() == len(HEAD) and version(writer_db) == HEAD
    command.downgrade(config(), PARENT)
    assert capacity() == len(HEAD) and not indexes(writer_db)
