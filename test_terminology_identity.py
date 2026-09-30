"""Identity tests. PostgreSQL cases use a new disposable LOCAL Docker container only."""

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
from threading import Barrier
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from alembic.migration import MigrationContext
from alembic.operations import Operations

from models import NphiesTerminology
from diagnosis_systems import ICD10_AM_SYSTEM
from seed_nphies_data import seed_terminologies
from import_icd10_am import import_dataset

DOCKER = ["docker", "--host", "npipe:////./pipe/docker_engine"]


@pytest.fixture(scope="session")
def postgres_engine():
    image_name = os.environ.get("TEST_POSTGRES_IMAGE", "postgres:16")
    if image_name not in {"postgres:16", "postgres:17"}:
        pytest.fail("TEST_POSTGRES_IMAGE must be postgres:16 or postgres:17.")
    if not shutil.which("docker"):
        pytest.skip("Isolated PostgreSQL unavailable: Docker missing.")
    try:
        check = subprocess.run(DOCKER + ["info"], capture_output=True, timeout=10)
        image = subprocess.run(
            DOCKER + ["image", "inspect", image_name],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Isolated PostgreSQL unavailable: local Docker unavailable.")
    if check.returncode or image.returncode:
        pytest.skip(
            "Isolated PostgreSQL unavailable: local daemon or selected PostgreSQL image missing."
        )
    name = "nphies-identity-test-" + uuid4().hex
    engine = None
    try:
        result = subprocess.run(
            DOCKER
            + [
                "run",
                "--rm",
                "-d",
                "--name",
                name,
                "-p",
                "127.0.0.1::5432",
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-e",
                "POSTGRES_DB=nphies_identity_test",
                image_name,
            ],
            capture_output=True,
            timeout=30,
        )
        assert (
            result.returncode == 0
        ), "Disposable PostgreSQL container failed to start."
        port = (
            subprocess.check_output(DOCKER + ["port", name, "5432/tcp"], text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        test_url = (
            f"postgresql+psycopg://postgres@127.0.0.1:{int(port)}/nphies_identity_test"
        )
        engine = sa.create_engine(
            test_url,
            connect_args={"connect_timeout": 2},
        )
        for _ in range(30):
            try:
                with engine.connect() as connection:
                    connection.execute(sa.text("SELECT 1"))
                break
            except sa.exc.OperationalError:
                time.sleep(0.2)
        else:
            pytest.fail("Disposable PostgreSQL did not become ready.")
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT current_database()"))
                == "nphies_identity_test"
            )
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv("TEST_DATABASE_URL", test_url)
            yield engine
    finally:
        if engine is not None:
            engine.dispose()
        subprocess.run(DOCKER + ["rm", "-f", name], capture_output=True, timeout=20)


@pytest.fixture(params=["sqlite", "postgresql"])
def engine(request):
    target = (
        sa.create_engine("sqlite://")
        if request.param == "sqlite"
        else request.getfixturevalue("postgres_engine")
    )
    yield target
    with target.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS nphies_terminology")
    if request.param == "sqlite":
        target.dispose()


@pytest.mark.parametrize("state", [{}, {"is_deleted": True}, {"is_active": False}])
def test_identity_reserved_in_every_state(engine, state):
    table = NphiesTerminology.__table__
    table.create(engine)
    with engine.begin() as connection:
        connection.execute(
            table.insert().values(code_system_url="test:one", code="A", **state)
        )
        connection.execute(table.insert().values(code_system_url="test:two", code="A"))
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                table.insert().values(code_system_url="test:one", code="A")
            )
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(table)) == 2


@pytest.mark.parametrize("column", ["code", "code_system_url"])
def test_null_identity_rejected(engine, column):
    table = NphiesTerminology.__table__
    table.create(engine)
    values = {"code": "A", "code_system_url": "test:one"}
    values[column] = None
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(table.insert().values(**values))


def migration():
    path = Path(__file__).parent / "alembic/versions/0005_terminology_identity.py"
    spec = importlib.util.spec_from_file_location("identity_migration_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.context = SimpleNamespace(is_offline_mode=lambda: False)
    return module


@pytest.mark.parametrize("bad", ["duplicates", "null", None])
def test_migration_preserves_or_refuses_existing_rows(engine, bad):
    old = sa.Table(
        "nphies_terminology",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code_system_url", sa.String),
        sa.Column("code", sa.String),
        sa.Column("display", sa.String),
        sa.Column("is_deleted", sa.Boolean),
        sa.Column("is_active", sa.Boolean),
    )
    old.create(engine)
    values = [
        dict(
            id=1,
            code_system_url="test:one",
            code="A",
            display="TEST",
            is_deleted=True,
            is_active=False,
        ),
        dict(
            id=2,
            code_system_url="test:two",
            code="A",
            display="TEST2",
            is_deleted=False,
            is_active=True,
        ),
    ]
    if bad == "duplicates":
        values[1]["code_system_url"] = "test:one"
    if bad == "null":
        values[1]["code"] = None
    with engine.begin() as connection:
        connection.execute(old.insert(), values)

    def upgrade():
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration().upgrade()

    if bad:
        with pytest.raises((RuntimeError, sa.exc.DBAPIError), match="require review"):
            upgrade()
        assert not sa.inspect(engine).get_unique_constraints("nphies_terminology")
    else:
        upgrade()
        assert any(
            c["name"] == "uq_terminology_identity"
            for c in sa.inspect(engine).get_unique_constraints("nphies_terminology")
        )
    with engine.connect() as connection:
        assert [
            dict(row)
            for row in connection.execute(sa.select(old).order_by(old.c.id)).mappings()
        ] == values


def test_postgres_concurrent_insert(postgres_engine):
    table = NphiesTerminology.__table__
    table.create(postgres_engine)
    barrier = Barrier(2)

    def insert():
        try:
            with postgres_engine.begin() as connection:
                barrier.wait(timeout=10)
                connection.execute(
                    table.insert().values(code_system_url="test:race", code="A")
                )
            return "inserted"
        except IntegrityError:
            return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(lambda _: insert(), range(2))) == [
                "conflict",
                "inserted",
            ]
        with postgres_engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(table)) == 1
    finally:
        table.drop(postgres_engine)


@pytest.mark.parametrize("kind", ["seed", "icd"])
def test_insertion_conflict_rolls_back_and_redacts(engine, tmp_path, capsys, kind):
    NphiesTerminology.__table__.create(engine)
    factory = sessionmaker(engine)
    path = tmp_path / "synthetic.json"
    concepts = [{"code": "E11.9", "display": "SYNTHETIC ONLY", "active": True}]
    if kind == "seed":
        data = {
            "resourceType": "CodeSystem",
            "url": ICD10_AM_SYSTEM,
            "concept": concepts,
        }
    else:
        data = {"system": ICD10_AM_SYSTEM, "concepts": concepts}
    path.write_text(json.dumps(data))

    def fail_after_flush(session, context):
        raise IntegrityError("PRIVATE_SENTINEL", {}, RuntimeError("PRIVATE_SENTINEL"))

    sa.event.listen(factory, "after_flush_postexec", fail_after_flush)
    try:
        report = (
            seed_terminologies(tmp_path, factory)
            if kind == "seed"
            else import_dataset(path, dry_run=False, session_factory=factory)
        )
    finally:
        sa.event.remove(factory, "after_flush_postexec", fail_after_flush)
    assert report.inserted == 0 and report.failed == 1
    if kind == "icd":
        assert report.would_insert == 0
        report.print_summary(False)
    assert "PRIVATE_SENTINEL" not in capsys.readouterr().out
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(NphiesTerminology.__table__)
            )
            == 0
        )


def test_migration_downgrade_preserves_rows_and_other_constraints(engine):
    old = sa.Table(
        "nphies_terminology",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code_system_url", sa.String),
        sa.Column("code", sa.String),
        sa.Column("display", sa.String),
        sa.Column("is_deleted", sa.Boolean, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
    )
    old.create(engine)
    sa.Index("ix_identity_test_display", old.c.display).create(engine)
    records = [
        dict(
            id=1,
            code_system_url="test:one",
            code="A",
            display="SYNTHETIC",
            is_deleted=False,
            is_active=True,
        ),
        dict(
            id=2,
            code_system_url="test:two",
            code="A",
            display="HISTORICAL",
            is_deleted=True,
            is_active=False,
        ),
    ]
    with engine.begin() as connection:
        connection.execute(old.insert(), records)
        with Operations.context(MigrationContext.configure(connection)):
            migration().upgrade()
    inspector = sa.inspect(engine)
    assert all(
        not c["nullable"]
        for c in inspector.get_columns(old.name)
        if c["name"] in {"code", "code_system_url"}
    )
    assert any(
        c["name"] == "uq_terminology_identity"
        for c in inspector.get_unique_constraints(old.name)
    )
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration().downgrade()
    inspector = sa.inspect(engine)
    assert not inspector.get_unique_constraints(old.name)
    assert inspector.get_pk_constraint(old.name)["constrained_columns"] == ["id"]
    assert any(
        i["name"] == "ix_identity_test_display" for i in inspector.get_indexes(old.name)
    )
    assert all(
        c["nullable"]
        for c in inspector.get_columns(old.name)
        if c["name"] in {"code", "code_system_url"}
    )
    with engine.connect() as connection:
        assert (
            list(connection.execute(sa.select(old).order_by(old.c.id)).mappings())
            == records
        )
    # A clean downgrade can be upgraded again without losing history.
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration().upgrade()
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                old.insert().values(
                    id=3,
                    code_system_url="test:one",
                    code="A",
                    is_deleted=False,
                    is_active=True,
                )
            )
