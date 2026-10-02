"""Phase 10A-1: isolated SQLite and disposable PostgreSQL only."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
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
from models import (
    Base,
    CoverageRuleHistory,
    DiagnosisCode,
    DiagnosisServiceRule,
    InsuranceCompany,
    ServiceCode,
    User,
)
from services.coverage_mutations import (
    CoverageMutationError,
    MutationContext,
    update_rule_coverage,
    validate_reason,
)
from test_terminology_identity import postgres_engine
from update_rule import main


@pytest.fixture(params=["sqlite", "postgresql"])
def history_db(request, monkeypatch):
    """Upgrade real migrations in a fresh SQLite DB or a disposable PG schema."""
    dialect, revision = (
        request.param if isinstance(request.param, tuple) else (request.param, "head")
    )
    root = None
    schema = None
    if dialect == "postgresql":
        root = request.getfixturevalue("postgres_engine")
        assert root.url.host == "127.0.0.1"
        assert root.url.database == "nphies_identity_test"
        schema = "history_test_" + uuid4().hex
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

    monkeypatch.setattr(database, "engine", engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    ownership = ExitStack()
    ownership.enter_context(
        owned_postgres_schema(engine, root, schema)
        if root is not None
        else owned_sqlite(engine)
    )
    try:
        cfg = Config(str(Path(__file__).with_name("alembic.ini")))
        command.upgrade(cfg, "0005_terminology_identity")
        with factory.begin() as db:
            db.add_all(
                [
                    DiagnosisCode(id=1, code="TEST-D", description="Synthetic"),
                    ServiceCode(id=1, code="TEST-S", description="Synthetic"),
                    InsuranceCompany(id=1, name="Synthetic insurer"),
                    User(
                        id=1,
                        username="synthetic",
                        role="admin",
                        password_hash="test-only",
                    ),
                ]
            )
            db.flush()
            historical_rules(
                db,
                *[
                    DiagnosisServiceRule(
                        id=1,
                        diagnosis_id=1,
                        service_id=1,
                        insurer_id=None,
                        is_covered=False,
                    ),
                    DiagnosisServiceRule(
                        id=2,
                        diagnosis_id=1,
                        service_id=1,
                        insurer_id=1,
                        is_covered=False,
                    ),
                    DiagnosisServiceRule(
                        id=3,
                        diagnosis_id=1,
                        service_id=1,
                        insurer_id=1,
                        is_covered=True,
                        is_deleted=True,
                    ),
                ],
            )
        with engine.connect() as connection:
            before = list(
                connection.execute(sa.select(DiagnosisServiceRule.__table__)).mappings()
            )
        command.upgrade(cfg, "0006_coverage_rule_history")
        with engine.connect() as connection:
            assert before == list(
                connection.execute(sa.select(DiagnosisServiceRule.__table__)).mappings()
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(CoverageRuleHistory)
                )
                == 0
            )
            assert (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                == "0006_coverage_rule_history"
            )
        command.upgrade(cfg, revision)
        yield factory
    finally:
        ownership.close()
        engine.dispose()
        if root is not None:
            with root.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')


def cli_args(scope=None):
    return [
        "--diagnosis-code",
        "TEST-D",
        "--service-code",
        "TEST-S",
        "--covered",
        "true",
        *(scope or ["--global"]),
    ]


def apply_args(scope=None):
    return cli_args(scope) + ["--apply", "--reason", "Synthetic coverage review"]


def history(factory):
    with factory() as db:
        return db.scalars(
            sa.select(CoverageRuleHistory).order_by(CoverageRuleHistory.id)
        ).all()


def covered(factory, rule_id=1):
    with factory() as db:
        return db.get(DiagnosisServiceRule, rule_id).is_covered


@pytest.mark.parametrize(
    "scope,rule_id", [(["--global"], 1), (["--insurer-id", "1"], 2)]
)
def test_cli_update_snapshot(history_db, scope, rule_id):
    assert main(apply_args(scope), session_factory=history_db) == 0
    rows = history(history_db)
    assert len(rows) == 1 and covered(history_db, rule_id)
    row = rows[0]
    assert row.rule_id == rule_id and row.action == "UPDATE"
    assert (row.diagnosis_id, row.service_id) == (1, 1)
    assert (row.diagnosis_code, row.service_code) == ("TEST-D", "TEST-S")
    assert row.insurer_id == (1 if rule_id == 2 else None)
    assert row.insurer_name == ("Synthetic insurer" if rule_id == 2 else None)
    assert row.old_is_covered is False and row.new_is_covered is True
    assert row.old_is_deleted is False and row.new_is_deleted is False
    assert row.actor_user_id is None and row.source == "update_rule.py"
    assert row.reason == "Synthetic coverage review"
    assert row.occurred_at is not None
    with history_db() as db:
        if db.bind.dialect.name == "postgresql":
            assert row.occurred_at.utcoffset() is not None


@pytest.mark.parametrize("flag", [[], ["--dry-run"]])
def test_dry_run_no_history(history_db, flag):
    assert main(cli_args() + flag, session_factory=history_db) == 0
    assert not covered(history_db) and history(history_db) == []


def test_unchanged_does_not_add_history(history_db, capsys):
    assert main(apply_args(), session_factory=history_db) == 0
    assert main(apply_args(), session_factory=history_db) == 0
    assert len(history(history_db)) == 1
    assert "Unchanged:" in capsys.readouterr().out


@pytest.mark.parametrize(
    "reason",
    [None, "", "   ", "\t\n ", "x" * 501, "  " + "x" * 501 + "  ", "line\nbreak"],
)
def test_apply_reason_rejected_before_connect(reason, capsys):
    def forbidden():
        pytest.fail("Invalid reason must never connect.")

    args = cli_args() + ["--apply"]
    if reason is not None:
        args += ["--reason", reason]
    with pytest.raises(SystemExit) as exc:
        main(args, session_factory=forbidden)
    assert exc.value.code == 2


def test_history_insert_failure_rolls_back_cli(history_db, capsys, caplog):
    with history_db() as db:
        engine = db.bind

    def reject(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO COVERAGE_RULE_HISTORY"):
            raise sa.exc.IntegrityError(
                "PRIVATE_SENTINEL", {}, RuntimeError("PRIVATE_SENTINEL")
            )

    sa.event.listen(engine, "before_cursor_execute", reject)
    try:
        assert main(apply_args(), session_factory=history_db) == 1
    finally:
        sa.event.remove(engine, "before_cursor_execute", reject)
    assert not covered(history_db) and history(history_db) == []
    output = capsys.readouterr().out
    assert "PRIVATE_SENTINEL" not in output and "Success:" not in output
    records = [r for r in caplog.records if r.name == "services.coverage_mutations"]
    assert len(records) == 1
    assert (
        records[0].getMessage()
        == "coverage_mutation_update_failed exception_type=IntegrityError"
    )
    assert records[0].exc_info is None and records[0].stack_info is None
    assert "PRIVATE_SENTINEL" not in caplog.text


def test_outer_rollback_removes_both(history_db):
    with pytest.raises(RuntimeError, match="outer rollback"):
        with history_db.begin() as db:
            assert update_rule_coverage(
                db, 1, True, context=MutationContext(reason="Synthetic")
            )
            assert (
                db.scalar(sa.select(sa.func.count()).select_from(CoverageRuleHistory))
                == 1
            )
            raise RuntimeError("outer rollback")
    assert not covered(history_db) and history(history_db) == []


@pytest.mark.parametrize(
    "operation", ["update", "delete", "soft_delete", "restore", "bulk_update"]
)
def test_history_orm_append_only(history_db, operation):
    assert main(apply_args(), session_factory=history_db) == 0
    with history_db() as db:
        row = db.scalar(sa.select(CoverageRuleHistory))
        with pytest.raises(ValueError, match="append-only"):
            if operation == "update":
                row.reason = "tampered"
                db.flush()
            elif operation == "delete":
                db.delete(row)
                db.flush()
            elif operation == "bulk_update":
                db.execute(sa.update(CoverageRuleHistory).values(reason="tampered"))
            else:
                getattr(row, operation)()
        db.rollback()
    assert history(history_db)[0].reason == "Synthetic coverage review"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE coverage_rule_history SET reason = 'tampered'",
        "DELETE FROM coverage_rule_history",
    ],
)
def test_history_raw_append_only(history_db, statement):
    assert main(apply_args(), session_factory=history_db) == 0
    with history_db() as db:
        engine = db.bind
    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        with engine.begin() as connection:
            connection.exec_driver_sql(statement)
    assert len(history(history_db)) == 1


def test_migration_metadata_and_no_backfill(history_db):
    with history_db() as db:
        assert (
            db.scalar(sa.select(sa.func.count()).select_from(CoverageRuleHistory)) == 0
        )
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(DiagnosisServiceRule)
                .execution_options(include_deleted=True)
            )
            == 3
        )
        assert (
            compare_metadata(MigrationContext.configure(db.connection()), Base.metadata)
            == []
        )
        assert (
            sa.inspect(db.bind).get_unique_constraints("diagnosis_service_rules") == []
        )


def test_trusted_actor_lifecycle_and_snapshots(history_db):
    with history_db.begin() as db:
        update_rule_coverage(
            db, 1, True, context=MutationContext(reason="Synthetic", actor_user_id=1)
        )
        user = db.get(User, 1)
        user.is_active = False
        user.token_version += 1
        user.soft_delete()
        db.get(DiagnosisCode, 1).code = "TEST-RENAMED"
    row = history(history_db)[0]
    assert row.actor_user_id == 1 and row.diagnosis_code == "TEST-D"
    with history_db() as db:
        engine = db.bind
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(sa.delete(User.__table__).where(User.id == 1))
    assert history(history_db)[0].actor_user_id == 1


def test_no_transaction_or_pending_identity_change(history_db):
    with history_db() as db:
        with pytest.raises(CoverageMutationError, match="caller-owned"):
            update_rule_coverage(
                db, 1, True, context=MutationContext(reason="Synthetic")
            )
    with history_db.begin() as db:
        row = db.get(DiagnosisServiceRule, 1)
        row.insurer_id = 1
        with pytest.raises(CoverageMutationError, match="pending changes"):
            update_rule_coverage(
                db, 1, True, context=MutationContext(reason="Synthetic")
            )
        db.rollback()
    assert not covered(history_db) and history(history_db) == []


def test_truncate_rejected(history_db):
    with history_db() as db:
        engine = db.bind
    if engine.dialect.name != "postgresql":
        return  # SQLite has no TRUNCATE; raw DELETE is tested above.
    assert main(apply_args(), session_factory=history_db) == 0
    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        with engine.begin() as connection:
            connection.exec_driver_sql("TRUNCATE coverage_rule_history")
    assert len(history(history_db)) == 1


@pytest.mark.parametrize("history_db", ["postgresql"], indirect=True)
def test_concurrent_updates_record_only_actual_change(history_db):
    barrier = Barrier(2)

    def update():
        with history_db.begin() as db:
            # Both sessions initially cache the same before-state. The service
            # must refresh it after acquiring the mutation lock.
            cached = db.get(DiagnosisServiceRule, 1)
            assert cached.is_covered is False
            barrier.wait(timeout=10)
            return update_rule_coverage(
                db,
                1,
                True,
                context=MutationContext(reason="Synthetic concurrent review"),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: update(), range(2)))
    assert sorted(results) == [False, True]
    rows = history(history_db)
    assert len(rows) == 1
    assert rows[0].old_is_covered is False and rows[0].new_is_covered is True
    assert covered(history_db)


@pytest.mark.parametrize(
    "raw, normalized",
    [
        ("  Synthetic review  ", "Synthetic review"),
        (" \t" + "x" * 500 + "\n ", "x" * 500),
    ],
)
def test_reason_normalized_before_validation_and_persisted(history_db, raw, normalized):
    assert validate_reason(raw) == normalized
    assert MutationContext(reason=raw).reason == normalized
    args = cli_args() + ["--apply", "--reason", raw]
    assert main(args, session_factory=history_db) == 0
    assert history(history_db)[0].reason == normalized


@pytest.mark.parametrize("history_db", ["postgresql"], indirect=True)
def test_lock_failure_logging_is_safe(history_db, capsys, caplog):
    with history_db() as db:
        engine = db.bind

    def reject(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("LOCK TABLE"):
            raise sa.exc.OperationalError(
                "PRIVATE_SQL",
                {"secret": "PRIVATE_PARAMETER"},
                RuntimeError("PRIVATE_CREDENTIAL"),
            )

    sa.event.listen(engine, "before_cursor_execute", reject)
    try:
        assert main(apply_args(), session_factory=history_db) == 1
    finally:
        sa.event.remove(engine, "before_cursor_execute", reject)
    assert not covered(history_db) and history(history_db) == []
    records = [r for r in caplog.records if r.name == "services.coverage_mutations"]
    assert len(records) == 1
    assert (
        records[0].getMessage()
        == "coverage_mutation_lock_failed exception_type=OperationalError"
    )
    assert records[0].exc_info is None and records[0].stack_info is None
    assert "PRIVATE_" not in caplog.text + capsys.readouterr().out
