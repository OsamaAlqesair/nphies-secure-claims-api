"""Catalog contracts on migrated SQLite and disposable PostgreSQL 17."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from io import StringIO
from threading import Barrier, Event
import time

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError

from models import (
    NphiesTerminology,
    TerminologyCatalog as Catalog,
    TerminologyCatalogEntry as Entry,
    utcnow,
)
from services import catalog_import as importer
from services.diagnosis_catalog import (
    diagnosis_catalog_readiness,
    DiagnosisCatalogMissingError,
)
from services.terminology import find_term, DIAGNOSIS_SYSTEM
from services.claim_business import ClaimBusinessEvaluator, BusinessPair
from test_coverage_writers import writer_db, config
from test_terminology_identity import postgres_engine


@pytest.fixture
def artifact(monkeypatch):
    entries = [
        dict(
            code="G43.9",
            display="SYNTHETIC TEST ONLY",
            display_confidence="flagged_ambiguous",
            source_line=1,
            classification="leaf_candidate",
        )
    ]
    manifest = replace(
        importer.TENTH_EDITION,
        edition="SYNTHETIC TEST ONLY",
        source_filename="synthetic.json",
        source_sha256=importer.digest("synthetic source"),
        expected_count=1,
        content_sha256=importer.digest(entries),
    )
    monkeypatch.setattr(importer, "REVIEWED_MANIFESTS", (manifest,))
    return dict(metadata=manifest.metadata(), entries=entries)


def load(factory, artifact, active=True):
    identity = importer.import_artifact(artifact, session_factory=factory)
    if active:
        importer.activate_catalog(identity, session_factory=factory)
    return identity


def test_lifecycle_legacy_leaf_and_replay(writer_db, artifact):
    with writer_db.begin() as db:
        db.add(NphiesTerminology(code_system_url=DIAGNOSIS_SYSTEM, code="G43"))
    with writer_db() as db:
        assert not diagnosis_catalog_readiness(db).present
        with pytest.raises(DiagnosisCatalogMissingError):
            find_term(db, "G43", (DIAGNOSIS_SYSTEM,))
    identity = load(writer_db, artifact, False)
    with writer_db() as db:
        assert not diagnosis_catalog_readiness(db).present
        row = db.get(Catalog, identity)
        assert row.state == "VALIDATED"
        assert row.source_sha256 != row.artifact_sha256 != row.content_sha256
        assert row.content_sha256 == row.import_sha256
    importer.activate_catalog(identity, session_factory=writer_db)
    assert load(writer_db, artifact) == identity
    with writer_db() as db:
        assert diagnosis_catalog_readiness(db).catalog.id == identity
        assert find_term(db, "G43", (DIAGNOSIS_SYSTEM,)) is None
        term = find_term(db, "G43.9", (DIAGNOSIS_SYSTEM,))
        assert term.display_confidence == "flagged_ambiguous"


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_sha256", "0" * 64),
        ("parser_version", "wrong"),
        ("edition", "official"),
        ("expected_count", 2),
        ("artifact_schema", "wrong"),
        ("code_system_url", "wrong"),
    ],
)
def test_unreviewed_metadata_fails_before_connection(artifact, field, value):
    artifact["metadata"][field] = value
    with pytest.raises(ValueError):
        importer.import_artifact(artifact, session_factory=None)


@pytest.mark.parametrize(
    "field,value",
    [
        ("code", "G43"),
        ("code", "G43-G44"),
        ("code", "M8000/3"),
        ("code", "G43.-"),
        ("classification", "nonassignable_parent_candidate"),
        ("display", "tampered"),
        ("source_line", True),
    ],
)
def test_changed_content_rejected(artifact, field, value):
    artifact["entries"][0][field] = value
    with pytest.raises(ValueError):
        importer.import_artifact(artifact, session_factory=None)


def test_duplicate_rejected(artifact):
    artifact["entries"] *= 2
    with pytest.raises(ValueError):
        importer.verify_artifact(artifact)


def test_unknown_catalog_never_ready(writer_db, artifact, monkeypatch):
    load(writer_db, artifact)
    monkeypatch.setattr(importer, "REVIEWED_MANIFESTS", (importer.TENTH_EDITION,))
    with writer_db() as db:
        assert not diagnosis_catalog_readiness(db).present


def test_same_identity_different_reviewed_content_conflicts(
    writer_db, artifact, monkeypatch
):
    identity = load(writer_db, artifact)
    changed = deepcopy(artifact)
    changed["entries"][0]["display"] = "DIFFERENT SYNTHETIC CONTENT"
    changed["metadata"]["content_sha256"] = importer.digest(changed["entries"])
    manifest = importer.ReviewedManifest(**changed["metadata"])
    monkeypatch.setattr(
        importer, "REVIEWED_MANIFESTS", (*importer.REVIEWED_MANIFESTS, manifest)
    )
    with pytest.raises(ValueError, match="conflicts"):
        load(writer_db, changed)
    with writer_db() as db:
        assert diagnosis_catalog_readiness(db).catalog.id == identity


def test_readiness_does_not_scan_entries(writer_db, artifact):
    load(writer_db, artifact)
    statements = []
    engine = writer_db.kw["bind"]

    def record(connection, cursor, statement, *args):
        statements.append(statement.lower())

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        with writer_db() as db:
            assert diagnosis_catalog_readiness(db).present
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert len(statements) == 1
    assert "terminology_catalog_entries" not in statements[0]
    assert "count(" not in statements[0]


def test_entry_uniqueness_and_foreign_key(writer_db, artifact):
    with writer_db.begin() as db:
        catalog = Catalog(**artifact["metadata"], artifact_sha256="a" * 64)
        db.add(catalog)
        db.flush()
        identity = catalog.id
    for catalog_id in (identity, 999999):
        with pytest.raises(DBAPIError):
            with writer_db.kw["bind"].begin() as connection:
                connection.execute(
                    Entry.__table__.insert(),
                    [dict(catalog_id=catalog_id, **artifact["entries"][0])] * 2,
                )


@pytest.mark.parametrize("writer_db", ["postgresql"], indirect=True)
def test_database_prevents_two_active_catalogs(writer_db, artifact, monkeypatch):
    load(writer_db, artifact)
    second = load(writer_db, second_artifact(artifact, monkeypatch), False)
    with pytest.raises(DBAPIError):
        with writer_db.kw["bind"].begin() as connection:
            connection.execute(
                Catalog.__table__.update()
                .where(Catalog.id == second)
                .values(state="ACTIVE", activated_at=utcnow())
            )


@pytest.mark.parametrize("writer_db", ["postgresql"], indirect=True)
@pytest.mark.parametrize(
    "table", ["terminology_catalogs", "terminology_catalog_entries"]
)
def test_postgres_truncate_guard(writer_db, artifact, table):
    load(writer_db, artifact)
    with pytest.raises(DBAPIError):
        with writer_db.kw["bind"].begin() as connection:
            connection.exec_driver_sql(f"TRUNCATE {table} CASCADE")


@pytest.mark.parametrize(
    "mutation",
    [
        "entry_update",
        "entry_delete",
        "entry_insert",
        "provenance",
        "evidence",
        "delete",
        "reactivate",
    ],
)
def test_sealed_database_guards(writer_db, artifact, mutation):
    identity = load(writer_db, artifact)
    table = Entry.__table__
    statements = {
        "entry_update": table.update().values(display="changed"),
        "entry_delete": table.delete(),
        "entry_insert": table.insert().values(
            catalog_id=identity, **dict(artifact["entries"][0], code="J00")
        ),
        "provenance": Catalog.__table__.update().values(edition="changed"),
        "evidence": Catalog.__table__.update().values(
            state="SUPERSEDED", import_sha256="0" * 64
        ),
        "delete": Catalog.__table__.delete(),
        "reactivate": Catalog.__table__.update().values(state="VALIDATED"),
    }
    with pytest.raises(DBAPIError):
        with writer_db.kw["bind"].begin() as connection:
            connection.execute(statements[mutation])
    with writer_db() as db:
        assert diagnosis_catalog_readiness(db).catalog.id == identity


def test_partial_failed_and_direct_active_insert(writer_db, artifact):
    metadata = artifact["metadata"]
    with writer_db.begin() as db:
        catalog = Catalog(**metadata, artifact_sha256="a" * 64)
        db.add(catalog)
        db.flush()
        identity = catalog.id
    with pytest.raises(DBAPIError):
        with writer_db.begin() as db:
            row = db.get(Catalog, identity)
            row.state = "VALIDATED"
            row.imported_count = 1
            row.import_sha256 = row.content_sha256
            row.validated_at = utcnow()
    with writer_db.begin() as db:
        assert not diagnosis_catalog_readiness(db).present
        db.get(Catalog, identity).state = "FAILED"
    with pytest.raises(ValueError):
        importer.activate_catalog(identity, session_factory=writer_db)
    with pytest.raises(ValueError):
        load(writer_db, artifact)
    with pytest.raises(DBAPIError):
        with writer_db.begin() as db:
            db.add(
                Catalog(
                    **dict(metadata, edition="other"),
                    artifact_sha256="a" * 64,
                    state="ACTIVE",
                    imported_count=1,
                    import_sha256=metadata["content_sha256"],
                    activated_at=utcnow(),
                    validated_at=utcnow(),
                )
            )


def second_artifact(artifact, monkeypatch):
    other = deepcopy(artifact)
    other["metadata"]["edition"] = "SYNTHETIC NEXT EDITION"
    other["entries"][0]["display"] = "SYNTHETIC DIFFERENT DISPLAY"
    other["metadata"]["content_sha256"] = importer.digest(other["entries"])
    manifest = importer.ReviewedManifest(**other["metadata"])
    monkeypatch.setattr(
        importer, "REVIEWED_MANIFESTS", (*importer.REVIEWED_MANIFESTS, manifest)
    )
    return other


def test_editions_coexist_and_request_pins_identity(writer_db, artifact, monkeypatch):
    first = load(writer_db, artifact)
    other = second_artifact(artifact, monkeypatch)
    with writer_db() as db:
        evaluator = ClaimBusinessEvaluator(db)
        before = evaluator.evaluate(BusinessPair("UNKNOWN", "UNKNOWN"))
        assert before.catalog.id == first
        second = load(writer_db, other)
        after = evaluator.evaluate(BusinessPair("G43.9", "UNKNOWN"))
        assert after.catalog.id == first
        assert after.diagnosis.display == artifact["entries"][0]["display"]
    with writer_db() as db:
        assert diagnosis_catalog_readiness(db).catalog.id == second
        assert db.get(Catalog, first).state == "SUPERSEDED"
        assert db.scalar(sa.select(sa.func.count()).select_from(Entry)) == 2
    with pytest.raises(ValueError):
        importer.activate_catalog(first, session_factory=writer_db)


def test_atomic_rollback_preserves_active(writer_db, artifact, monkeypatch):
    first = load(writer_db, artifact)
    other = second_artifact(artifact, monkeypatch)
    engine = writer_db.kw["bind"]

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO terminology_catalog_entries"):
            raise RuntimeError("Injected entry write failure")

    sa.event.listen(engine, "after_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            load(writer_db, other)
    finally:
        sa.event.remove(engine, "after_cursor_execute", fail)
    with writer_db() as db:
        assert diagnosis_catalog_readiness(db).catalog.id == first
        assert db.scalar(sa.select(sa.func.count()).select_from(Catalog)) == 1
        assert db.scalar(sa.select(sa.func.count()).select_from(Entry)) == 1


def test_guarded_downgrade(writer_db, artifact):
    load(writer_db, artifact)
    with pytest.raises(RuntimeError, match="Refusing downgrade"):
        command.downgrade(config(), "0010_claim_intake_history")


def test_empty_downgrade_upgrade(writer_db):
    command.downgrade(config(), "0010_claim_intake_history")
    command.upgrade(config(), "head")


@pytest.mark.parametrize("writer_db", ["postgresql"], indirect=True)
@pytest.mark.parametrize("first", ["import", "downgrade"])
def test_concurrent_import_and_downgrade_preserves_history(writer_db, artifact, first):
    engine = writer_db.kw["bind"]
    first_paused, waiter_started, release_first = Event(), Event(), Event()
    pids = {}

    def pause_first(connection, cursor, statement, parameters, context, executemany):
        importing = statement.startswith("INSERT INTO terminology_catalogs")
        checking = statement == "SELECT count(*) FROM terminology_catalogs"
        if (first == "import" and importing) or (first == "downgrade" and checking):
            pids["holder"] = cursor.connection.info.backend_pid
            first_paused.set()
            assert release_first.wait(15), "First transaction was not released."

    def observe_waiter(connection, cursor, statement, parameters, context, executemany):
        locking = statement.startswith("LOCK TABLE terminology_catalogs")
        finding = (
            "FROM terminology_catalogs" in statement
            and statement.startswith("SELECT ")
            and "count(*)" not in statement
        )
        if (first == "import" and locking) or (first == "downgrade" and finding):
            pids["waiter"] = cursor.connection.info.backend_pid
            waiter_started.set()

    def run_import():
        return importer.import_artifact(artifact, session_factory=writer_db)

    def run_downgrade():
        command.downgrade(config(), "0010_claim_intake_history")

    sa.event.listen(engine, "after_cursor_execute", pause_first)
    sa.event.listen(engine, "before_cursor_execute", observe_waiter)
    try:
        with ThreadPoolExecutor(2) as pool:
            leader = pool.submit(run_import if first == "import" else run_downgrade)
            try:
                assert first_paused.wait(
                    10
                ), "First transaction did not reach its checkpoint."
                waiter = pool.submit(run_downgrade if first == "import" else run_import)
                assert waiter_started.wait(10), "Competing transaction did not start."
                deadline = time.monotonic() + 5
                blocked = False
                with engine.connect() as connection:
                    while time.monotonic() < deadline and not waiter.done():
                        blocked = connection.exec_driver_sql(
                            "SELECT %s = ANY(pg_blocking_pids(%s))",
                            (pids["holder"], pids["waiter"]),
                        ).scalar()
                        if blocked:
                            break
                        time.sleep(0.02)
                assert blocked, "Downgrade and import must exclude each other's writes."
            finally:
                release_first.set()
            leader.result(timeout=15)
            if first == "import":
                with pytest.raises(RuntimeError, match="Refusing downgrade"):
                    waiter.result(timeout=15)
            else:
                with pytest.raises(DBAPIError):
                    waiter.result(timeout=15)
    finally:
        release_first.set()
        sa.event.remove(engine, "after_cursor_execute", pause_first)
        sa.event.remove(engine, "before_cursor_execute", observe_waiter)

    if first == "downgrade":
        command.upgrade(config(), "head")
    with writer_db() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(Catalog)) == (
            1 if first == "import" else 0
        )
        assert db.scalar(sa.select(sa.func.count()).select_from(Entry)) == (
            1 if first == "import" else 0
        )
        assert (
            db.connection()
            .exec_driver_sql("SELECT version_num FROM alembic_version")
            .scalar()
            == "0011_terminology_catalogs"
        )


@pytest.mark.parametrize(
    "writer_db",
    [
        ("sqlite", "0010_claim_intake_history"),
        ("postgresql", "0010_claim_intake_history"),
    ],
    indirect=True,
)
def test_upgrade_preserves_legacy_identity_without_trusting_it(writer_db):
    with writer_db.begin() as db:
        db.add_all(
            [
                NphiesTerminology(
                    id=123,
                    code_system_url=DIAGNOSIS_SYSTEM,
                    code="G43",
                    display="Legacy unknown provenance",
                ),
                NphiesTerminology(
                    id=124,
                    code_system_url="urn:service",
                    code="SERVICE",
                    display="Preserved service",
                ),
            ]
        )
    command.upgrade(config(), "head")
    with writer_db() as db:
        assert [
            (r.id, r.display)
            for r in db.scalars(
                sa.select(NphiesTerminology).order_by(NphiesTerminology.id)
            )
        ] == [(123, "Legacy unknown provenance"), (124, "Preserved service")]
        assert not diagnosis_catalog_readiness(db).present
        assert db.scalar(sa.select(sa.func.count()).select_from(Catalog)) == 0


def test_catalog_migration_offline_postgres_sql():
    output = StringIO()
    configuration = config()
    configuration.output_buffer = output
    command.upgrade(configuration, "0010_claim_intake_history:head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE terminology_catalogs" in sql
    assert "CREATE UNIQUE INDEX uq_catalog_active_family" in sql
    assert "CREATE TRIGGER terminology_entry_guard" in sql
    assert "FOR UPDATE" in sql


def test_mixed_system_lookup_cannot_use_legacy_icd(writer_db):
    with writer_db.begin() as db:
        db.add(NphiesTerminology(code_system_url=DIAGNOSIS_SYSTEM, code="G43"))
    with writer_db() as db, pytest.raises(ValueError, match="explicit catalog scope"):
        find_term(db, "G43", (DIAGNOSIS_SYSTEM, "urn:service"))


@pytest.mark.parametrize("writer_db", ["postgresql"], indirect=True)
def test_concurrent_import_and_activation(writer_db, artifact, monkeypatch):
    barrier = Barrier(2)

    def run_import():
        barrier.wait(timeout=10)
        return load(writer_db, artifact, False)

    with ThreadPoolExecutor(2) as pool:
        identities = list(pool.map(lambda _: run_import(), range(2)))
    assert identities[0] == identities[1]
    other = second_artifact(artifact, monkeypatch)
    second = load(writer_db, other, False)
    barrier = Barrier(2)

    def run_activate(identity):
        barrier.wait(timeout=10)
        return importer.activate_catalog(identity, session_factory=writer_db)

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(run_activate, [identities[0], second]))
    with writer_db() as db:
        assert sorted(db.scalars(sa.select(Catalog.state))) == ["ACTIVE", "SUPERSEDED"]


@pytest.mark.parametrize("writer_db", ["postgresql"], indirect=True)
def test_real_source_postgres(writer_db, monkeypatch):
    from services.icd10_am_reconstruction import parse_file

    source = Path("source_data/raw/1015865188-ICD-10-AM-Tabular-List.txt")
    if not source.exists():
        pytest.skip("Local licensed source is not bundled.")
    result = parse_file(source)
    assert result.source.sha256 == importer.TENTH_EDITION.source_sha256
    assert len(result.leaves) == 16953 and len(result.parents) == 2860
    assert result.report.counts.chapters_detected == 22
    assert result.report.counts.leaf_display_flags == 5565
    identity = load(writer_db, importer.reconstruction_artifact(result))
    assert load(writer_db, importer.reconstruction_artifact(result)) == identity
    with writer_db() as db:
        assert find_term(db, "G43", (DIAGNOSIS_SYSTEM,)) is None
        assert find_term(db, "G43.9", (DIAGNOSIS_SYSTEM,)) is not None
        assert diagnosis_catalog_readiness(db).active_rows == 16953
    artifact = importer.reconstruction_artifact(result)
    artifact["metadata"]["parser_version"] = "SYNTHETIC-rollback-test"
    manifest = importer.ReviewedManifest(**artifact["metadata"])
    monkeypatch.setattr(
        importer, "REVIEWED_MANIFESTS", (importer.TENTH_EDITION, manifest)
    )
    # Fail inside executemany at entry 12,000, after thousands of actual writes.
    failing_code = artifact["entries"][11999]["code"]
    assert __import__("re").fullmatch(r"[A-Z][0-9]{2}(?:\.[0-9]{1,2})?", failing_code)
    with writer_db.kw["bind"].begin() as connection:
        connection.exec_driver_sql(f"""
        CREATE FUNCTION fail_catalog_test_entry() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.code = '{failing_code}' THEN RAISE EXCEPTION 'Injected test failure'; END IF; RETURN NEW; END $$;
        CREATE TRIGGER fail_catalog_test_entry BEFORE INSERT ON terminology_catalog_entries
        FOR EACH ROW EXECUTE FUNCTION fail_catalog_test_entry();
        """)
    with pytest.raises(DBAPIError):
        load(writer_db, artifact)
    with writer_db() as db:
        assert diagnosis_catalog_readiness(db).catalog.id == identity
        assert db.scalar(sa.select(sa.func.count()).select_from(Catalog)) == 1
        assert db.scalar(sa.select(sa.func.count()).select_from(Entry)) == 16953
