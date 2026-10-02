"""Audited seed and legacy import: isolated SQLite and disposable PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import sqlite3
from threading import Barrier
from uuid import uuid4

import pytest
from contextlib import ExitStack
from testing_coverage import owned_sqlite, owned_postgres_schema, historical_rules
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

import database
import seed
from migrate_sqlite import IMPORT_REASON, LEGACY_TABLES, import_legacy
from models import (
    Base,
    CoverageRuleHistory,
    DiagnosisCode,
    DiagnosisServiceRule,
    InsuranceCompany,
    ServiceCode,
)
from services.coverage_mutations import (
    CoverageMutationError,
    MutationContext,
    create_rule,
    import_rule,
)
from test_terminology_identity import postgres_engine


@pytest.fixture(params=["sqlite", "postgresql"])
def writer_db(request, monkeypatch):
    # Fixed-revision migration tests must not cross later history-preserving
    # downgrades. Other cases continue to exercise the current repository head.
    dialect, revision = (
        request.param if isinstance(request.param, tuple) else (request.param, "head")
    )
    root = schema = None
    if dialect == "postgresql":
        root = request.getfixturevalue("postgres_engine")
        assert root.url.host == "127.0.0.1"
        assert root.url.database == "nphies_identity_test"
        schema = "writer_test_" + uuid4().hex
        with root.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = sa.create_engine(
            root.url, connect_args={"options": f"-csearch_path={schema}"}
        )
    else:
        engine = sa.create_engine("sqlite://")

        @sa.event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    factory = sessionmaker(engine, expire_on_commit=False)
    ownership = ExitStack()
    ownership.enter_context(
        owned_postgres_schema(engine, root, schema)
        if root is not None
        else owned_sqlite(engine)
    )
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(seed, "SessionLocal", factory)
    try:
        command.upgrade(config(), revision)
        yield factory
    finally:
        ownership.close()
        engine.dispose()
        if root is not None:
            with root.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')


def config():
    return Config(str(Path(__file__).with_name("alembic.ini")))


def snapshot(factory):
    with factory.kw["bind"].connect() as connection:
        return {
            name: connection.execute(
                sa.select(Base.metadata.tables[name]).order_by(
                    Base.metadata.tables[name].c.id
                )
            ).all()
            for name in (*LEGACY_TABLES, "coverage_rule_history")
        }


def rows(factory, model):
    with factory() as db:
        return db.scalars(
            sa.select(model).order_by(model.id).execution_options(include_deleted=True)
        ).all()


def mappings(factory, *, deleted=False):
    with factory.begin() as db:
        db.add_all(
            [
                DiagnosisCode(
                    id=7, code="TEST-D", description="Synthetic", is_deleted=deleted
                ),
                ServiceCode(
                    id=8, code="TEST-S", description="Synthetic", is_deleted=deleted
                ),
                InsuranceCompany(id=9, name="Synthetic insurer", is_deleted=deleted),
            ]
        )


def import_context():
    return MutationContext(reason=IMPORT_REASON, source="migrate_sqlite.py")


def legacy_source(tmp_path, *, deleted_mappings=False, timestamps=False):
    source = tmp_path / "synthetic_legacy.sqlite"
    timestamp_columns = ", created_at TEXT, updated_at TEXT" if timestamps else ""
    with sqlite3.connect(source) as legacy:
        legacy.executescript(f"""
            CREATE TABLE diagnosis_codes(id INTEGER PRIMARY KEY, code TEXT, description TEXT, is_deleted BOOLEAN);
            CREATE TABLE service_codes(id INTEGER PRIMARY KEY, code TEXT, description TEXT, is_deleted BOOLEAN);
            CREATE TABLE insurance_companies(id INTEGER PRIMARY KEY, name TEXT, is_deleted BOOLEAN);
            CREATE TABLE diagnosis_service_rules(id INTEGER PRIMARY KEY, diagnosis_id INTEGER, service_id INTEGER,
                insurer_id INTEGER, is_covered BOOLEAN, is_deleted BOOLEAN{timestamp_columns});
        """)
        legacy.execute(
            "INSERT INTO diagnosis_codes VALUES(7, 'TEST-D', 'Synthetic', ?)",
            (deleted_mappings,),
        )
        legacy.execute(
            "INSERT INTO service_codes VALUES(8, 'TEST-S', 'Synthetic', ?)",
            (deleted_mappings,),
        )
        legacy.execute(
            "INSERT INTO insurance_companies VALUES(9, 'Synthetic insurer', ?)",
            (deleted_mappings,),
        )
        for rule in [
            (10, 7, 8, None, True, False),
            (11, 7, 8, 9, False, False),
            (12, 7, 8, None, False, True),
            (13, 7, 8, 9, True, True),
        ]:
            if timestamps:
                legacy.execute(
                    "INSERT INTO diagnosis_service_rules VALUES(?,?,?,?,?,?,?,?)",
                    (*rule, "2000-01-02T03:04:05+03:00", "2001-02-03T04:05:06+03:00"),
                )
            else:
                legacy.execute(
                    "INSERT INTO diagnosis_service_rules VALUES(?,?,?,?,?,?)", rule
                )
    return source


def aware(value):
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def test_seed_new_rules_history_and_idempotency(writer_db):
    engine = writer_db.kw["bind"]
    commits = []

    def committed(connection):
        commits.append(True)

    sa.event.listen(engine, "commit", committed)
    try:
        seed.seed_data()
    finally:
        sa.event.remove(engine, "commit", committed)
    assert len(commits) == 1
    histories = rows(writer_db, CoverageRuleHistory)
    rules = rows(writer_db, DiagnosisServiceRule)
    assert len(histories) == len(rules) == 3
    assert [
        (h.diagnosis_code, h.service_code, h.insurer_name, h.new_is_covered)
        for h in histories
    ] == [
        ("J00", "70450", None, True),
        ("G43", "70450", None, True),
        ("G43", "70450", "Bupa", False),
    ]
    for rule, history in zip(rules, histories):
        assert history.rule_id == rule.id
        assert (history.diagnosis_id, history.service_id, history.insurer_id) == (
            rule.diagnosis_id,
            rule.service_id,
            rule.insurer_id,
        )
        assert history.action == "CREATE" and history.source == "seed.py"
        assert history.reason == seed.SEED_REASON and history.actor_user_id is None
        assert history.old_is_covered is None and history.old_is_deleted is None
        assert history.new_is_deleted is False and history.occurred_at is not None
    before = snapshot(writer_db)
    seed.seed_data()
    assert snapshot(writer_db) == before


@pytest.mark.parametrize("deleted", [False, True])
def test_seed_preserves_existing_rule_and_duplicates(writer_db, deleted):
    with writer_db.begin() as db:
        diagnosis = DiagnosisCode(code="J00", description="Synthetic")
        service = ServiceCode(code="70450", description="Synthetic")
        db.add_all([diagnosis, service])
        db.flush()
        historical_rules(
            db,
            *[
                DiagnosisServiceRule(
                    diagnosis_id=diagnosis.id,
                    service_id=service.id,
                    is_covered=False,
                    is_deleted=True,
                ),
                DiagnosisServiceRule(
                    diagnosis_id=diagnosis.id,
                    service_id=service.id,
                    is_covered=False,
                    is_deleted=deleted,
                ),
            ],
        )
    original = snapshot(writer_db)["diagnosis_service_rules"]
    seed.seed_data()
    assert snapshot(writer_db)["diagnosis_service_rules"][:2] == original
    histories = rows(writer_db, CoverageRuleHistory)
    assert len(histories) == 2 and all(h.diagnosis_code == "G43" for h in histories)
    before = snapshot(writer_db)
    seed.seed_data()
    assert snapshot(writer_db) == before


@pytest.mark.parametrize(
    "model,values",
    [
        (DiagnosisCode, dict(code="J00", description="Synthetic")),
        (DiagnosisCode, dict(code="G43", description="Synthetic")),
        (ServiceCode, dict(code="70450", description="Synthetic")),
        (InsuranceCompany, dict(name="Bupa")),
    ],
)
def test_seed_deleted_mapping_fails_closed_and_rolls_back(writer_db, model, values):
    with writer_db.begin() as db:
        db.add(model(is_deleted=True, **values))
    before = snapshot(writer_db)
    with pytest.raises(CoverageMutationError) as error:
        seed.seed_data()
    assert error.value.code == "rule_mapping_deleted"
    assert snapshot(writer_db) == before
    assert rows(writer_db, model)[0].is_deleted is True


@pytest.mark.parametrize("writer", ["seed", "import"])
def test_history_failure_rolls_back_complete_writer(
    writer_db, tmp_path, writer, caplog, capsys
):
    source = legacy_source(tmp_path)
    digest = sha256(source.read_bytes()).digest()
    engine = writer_db.kw["bind"]
    before = snapshot(writer_db)
    attempts = []

    def reject(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO COVERAGE_RULE_HISTORY"):
            attempts.append(True)
            if len(attempts) == 2:
                raise sa.exc.IntegrityError(
                    "PRIVATE_SQL",
                    {"secret": "PRIVATE_VALUE"},
                    RuntimeError("PRIVATE_VALUE"),
                )

    sa.event.listen(engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(CoverageMutationError) as error:
            if writer == "seed":
                seed.seed_data()
            else:
                import_legacy(source, engine)
    finally:
        sa.event.remove(engine, "before_cursor_execute", reject)
    assert error.value.code == "rule_mutation_failed" and len(attempts) == 2
    assert "PRIVATE_" not in str(error.value) + caplog.text + capsys.readouterr().out
    assert snapshot(writer_db) == before
    assert sha256(source.read_bytes()).digest() == digest
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("deleted_mappings", [False, True])
@pytest.mark.parametrize("timestamps", [False, True])
def test_legacy_import_state_history_timestamps_and_preconditions(
    writer_db,
    tmp_path,
    deleted_mappings,
    timestamps,
):
    source = legacy_source(
        tmp_path, deleted_mappings=deleted_mappings, timestamps=timestamps
    )
    digest = sha256(source.read_bytes()).digest()
    engine = writer_db.kw["bind"]
    commits = []

    def committed(connection):
        commits.append(True)

    sa.event.listen(engine, "commit", committed)
    started = datetime.now(timezone.utc)
    try:
        counts = import_legacy(source, engine)
    finally:
        sa.event.remove(engine, "commit", committed)
    finished = datetime.now(timezone.utc)
    assert len(commits) == 1
    assert counts == dict(
        diagnosis_codes=1,
        service_codes=1,
        insurance_companies=1,
        diagnosis_service_rules=4,
    )
    assert sha256(source.read_bytes()).digest() == digest
    rules = rows(writer_db, DiagnosisServiceRule)
    histories = rows(writer_db, CoverageRuleHistory)
    assert [(r.id, r.is_covered, r.is_deleted, r.insurer_id) for r in rules] == [
        (10, True, False, None),
        (11, False, False, 9),
        (12, False, True, None),
        (13, True, True, 9),
    ]
    assert len(histories) == 4
    for rule, history in zip(rules, histories):
        assert history.rule_id == rule.id and history.action == "CREATE"
        assert history.old_is_covered is None and history.old_is_deleted is None
        assert (
            history.new_is_covered is rule.is_covered
            and history.new_is_deleted is rule.is_deleted
        )
        assert (history.diagnosis_id, history.service_id, history.insurer_id) == (
            7,
            8,
            rule.insurer_id,
        )
        assert (history.diagnosis_code, history.service_code) == ("TEST-D", "TEST-S")
        assert history.insurer_name == (
            "Synthetic insurer" if rule.insurer_id else None
        )
        assert history.source == "migrate_sqlite.py" and history.reason == IMPORT_REASON
        assert history.actor_user_id is None
        assert started <= aware(history.occurred_at) <= finished
        if timestamps:
            for field, expected in [
                ("created_at", "2000-01-02T03:04:05+03:00"),
                ("updated_at", "2001-02-03T04:05:06+03:00"),
            ]:
                actual, original = getattr(rule, field), datetime.fromisoformat(
                    expected
                )
                if engine.dialect.name == "sqlite":
                    assert actual == original.replace(tzinfo=None)
                else:
                    assert actual == original
    for model in [DiagnosisCode, ServiceCode, InsuranceCompany]:
        assert rows(writer_db, model)[0].is_deleted is deleted_mappings
    before = snapshot(writer_db)
    with pytest.raises(RuntimeError, match="not empty"):
        import_legacy(source, engine)
    assert snapshot(writer_db) == before


@pytest.mark.parametrize(
    "missing_table", ["diagnosis_codes", "service_codes", "insurance_companies"]
)
def test_missing_import_mapping_rolls_back_everything(
    writer_db, tmp_path, missing_table
):
    source = legacy_source(tmp_path)
    with sqlite3.connect(source) as legacy:
        legacy.execute(f"DELETE FROM {missing_table}")
    digest = sha256(source.read_bytes()).digest()
    before = snapshot(writer_db)
    with pytest.raises(CoverageMutationError) as error:
        import_legacy(source, writer_db.kw["bind"])
    assert error.value.code == "rule_mapping_missing"
    assert snapshot(writer_db) == before
    assert sha256(source.read_bytes()).digest() == digest


def test_import_service_no_commit_and_outer_rollback(writer_db):
    mappings(writer_db, deleted=True)
    before = snapshot(writer_db)
    with pytest.raises(RuntimeError, match="outer rollback"):
        with writer_db.begin() as db:
            rule = import_rule(
                db,
                123,
                7,
                8,
                False,
                insurer_id=9,
                is_deleted=True,
                context=import_context(),
            )
            assert rule.id == 123 and rule.is_deleted is True
            assert db.in_transaction()
            assert (
                db.scalar(sa.select(sa.func.count()).select_from(CoverageRuleHistory))
                == 1
            )
            raise RuntimeError("outer rollback")
    assert snapshot(writer_db) == before


def test_import_requires_caller_transaction(writer_db):
    with writer_db() as db:
        with pytest.raises(CoverageMutationError, match="caller-owned"):
            import_rule(db, 123, 7, 8, True, context=import_context())


@pytest.mark.parametrize(
    "field,value",
    [
        ("rule_id", None),
        ("is_covered", 1),
        ("is_deleted", 1),
        ("created_at", "PRIVATE_INVALID_TIME"),
        ("updated_at", None),
    ],
)
def test_import_invalid_input_has_safe_error(writer_db, field, value):
    mappings(writer_db)
    before = snapshot(writer_db)
    args = dict(
        rule_id=123,
        diagnosis_id=7,
        service_id=8,
        is_covered=True,
        context=import_context(),
    )
    args[field] = value
    with pytest.raises(CoverageMutationError) as error:
        with writer_db.begin() as db:
            import_rule(db, **args)
    assert "PRIVATE_" not in str(error.value)
    assert snapshot(writer_db) == before


@pytest.mark.parametrize(
    "source",
    ["update_rule.py", "coverage_mutations.py", "seed.py", "migrate_sqlite.py"],
)
def test_all_truthful_sources_accepted(writer_db, source):
    mappings(writer_db)
    with writer_db.begin() as db:
        create_rule(
            db,
            7,
            8,
            True,
            context=MutationContext(reason="Synthetic source check", source=source),
        )
    assert rows(writer_db, CoverageRuleHistory)[0].source == source


@pytest.mark.parametrize("source", ["seed.py", "migrate_sqlite.py"])
@pytest.mark.parametrize(
    "writer_db",
    [
        ("sqlite", "0009_coverage_rule_current_identity"),
        ("postgresql", "0009_coverage_rule_current_identity"),
    ],
    indirect=True,
)
def test_writer_source_migration_preserves_history_and_refuses_destructive_downgrade(
    writer_db, source
):
    engine = writer_db.kw["bind"]
    command.downgrade(config(), "0007_coverage_history_sources")
    mappings(writer_db)
    with writer_db.begin() as db:
        db.add(ServiceCode(id=18, code="WRITER-S", description="Synthetic"))
    with writer_db.begin() as db:
        for scope, old_source in [
            (None, "update_rule.py"),
            (9, "coverage_mutations.py"),
        ]:
            create_rule(
                db,
                7,
                8,
                False,
                insurer_id=scope,
                context=MutationContext(
                    reason="Synthetic prior event", source=old_source
                ),
            )
    before = snapshot(writer_db)
    command.upgrade(config(), "0008_coverage_writer_sources")
    assert snapshot(writer_db) == before
    with engine.connect() as connection:
        # Intake history did not exist at this deliberately fixed checkpoint.
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
            all(
                diff[0] == "add_index"
                and diff[1].name
                in (
                    "uq_diagnosis_service_rule_current_global",
                    "uq_diagnosis_service_rule_current_insurer",
                )
                for diff in compare_metadata(checkpoint, Base.metadata)
            )
            and len(compare_metadata(checkpoint, Base.metadata)) == 2
        )
    inspector = sa.inspect(engine)
    assert inspector.get_unique_constraints("diagnosis_service_rules") == []
    assert not any(
        index["unique"] for index in inspector.get_indexes("diagnosis_service_rules")
    )
    command.downgrade(config(), "0007_coverage_history_sources")
    assert snapshot(writer_db) == before
    command.upgrade(config(), "0008_coverage_writer_sources")
    with writer_db.begin() as db:
        create_rule(
            db,
            7,
            18,
            True,
            context=MutationContext(reason="Synthetic writer event", source=source),
        )
    before = snapshot(writer_db)
    with pytest.raises(RuntimeError, match="history must be preserved"):
        command.downgrade(config(), "0007_coverage_history_sources")
    assert snapshot(writer_db) == before
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0008_coverage_writer_sources"
        )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE coverage_rule_history SET reason = 'tampered'",
        "DELETE FROM coverage_rule_history",
        "TRUNCATE coverage_rule_history",
    ],
)
def test_append_only_protection_after_writer_migration(writer_db, statement):
    engine = writer_db.kw["bind"]
    if statement.startswith("TRUNCATE") and engine.dialect.name != "postgresql":
        pytest.skip("SQLite has no TRUNCATE; PostgreSQL verifies it.")
    seed.seed_data()
    before = snapshot(writer_db)
    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        with engine.begin() as connection:
            connection.exec_driver_sql(statement)
    assert snapshot(writer_db) == before


def test_unknown_history_source_rejected(writer_db):
    seed.seed_data()
    rule = rows(writer_db, DiagnosisServiceRule)[0]
    before = snapshot(writer_db)
    with pytest.raises(sa.exc.IntegrityError):
        with writer_db.kw["bind"].begin() as connection:
            connection.execute(
                CoverageRuleHistory.__table__.insert().values(
                    rule_id=rule.id,
                    action="CREATE",
                    diagnosis_id=rule.diagnosis_id,
                    service_id=rule.service_id,
                    diagnosis_code="J00",
                    service_code="70450",
                    old_is_covered=None,
                    old_is_deleted=None,
                    new_is_covered=True,
                    new_is_deleted=False,
                    source="unknown",
                    reason="Synthetic source check",
                )
            )
    assert snapshot(writer_db) == before


@pytest.mark.parametrize("writer_db", ["postgresql"], indirect=True)
def test_postgres_preserves_native_legacy_timestamp_formats(writer_db, tmp_path):
    source = legacy_source(tmp_path, timestamps=True)
    supplied = "2000/01/02 03:04:05+03"
    with sqlite3.connect(source) as legacy:
        legacy.execute("UPDATE diagnosis_service_rules SET created_at = ?", (supplied,))
    digest = sha256(source.read_bytes()).digest()
    engine = writer_db.kw["bind"]
    with engine.connect() as connection:
        expected = connection.scalar(
            sa.text("SELECT CAST(:supplied AS TIMESTAMP WITH TIME ZONE)"),
            {"supplied": supplied},
        )
    import_legacy(source, engine)
    assert all(
        rule.created_at == expected for rule in rows(writer_db, DiagnosisServiceRule)
    )
    assert sha256(source.read_bytes()).digest() == digest


@pytest.mark.parametrize("writer_db", ["postgresql"], indirect=True)
def test_postgres_sequences_preserved(writer_db, tmp_path):
    import_legacy(legacy_source(tmp_path), writer_db.kw["bind"])
    with writer_db.begin() as db:
        diagnosis = DiagnosisCode(code="NEXT-D", description="Synthetic")
        service = ServiceCode(code="NEXT-S", description="Synthetic")
        insurer = InsuranceCompany(name="Next synthetic insurer")
        db.add_all([diagnosis, service, insurer])
        db.flush()
        assert (diagnosis.id, service.id, insurer.id) == (8, 9, 10)
        rule = create_rule(
            db,
            diagnosis.id,
            service.id,
            False,
            context=MutationContext(reason="Synthetic next rule"),
        )
        assert rule.id == 14


@pytest.mark.parametrize("writer_db", ["postgresql"], indirect=True)
@pytest.mark.parametrize("writer", ["seed", "import"])
def test_postgres_writer_serialization(writer_db, tmp_path, writer):
    source = legacy_source(tmp_path)
    barrier = Barrier(2)

    def write():
        barrier.wait(timeout=10)
        try:
            if writer == "seed":
                seed.seed_data()
            else:
                import_legacy(source, writer_db.kw["bind"])
            return "success"
        except RuntimeError as error:
            assert "not empty" in str(error)
            return "precondition"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: write(), range(2)))
    assert sorted(results) == (
        ["success", "success"] if writer == "seed" else ["precondition", "success"]
    )
    assert len(rows(writer_db, CoverageRuleHistory)) == (3 if writer == "seed" else 4)
