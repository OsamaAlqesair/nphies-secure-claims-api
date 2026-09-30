"""Synthetic TEST-ONLY records; no approved diagnosis catalog is bundled.

These labels are deliberately synthetic and not clinical descriptions.
Files are generated only under pytest's temporary directory, never nphies_data.
"""

import json
from pathlib import Path
import pytest
from sqlalchemy import create_engine, select, func, event
from sqlalchemy.orm import sessionmaker

from diagnosis_systems import ICD10_AM_SYSTEM
from models import NphiesTerminology
from services.diagnosis_catalog import (
    diagnosis_catalog_readiness,
    DiagnosisCatalogMissingError,
)
from services.terminology import find_term
from import_icd10_am import import_dataset, load_approved_dataset


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    NphiesTerminology.__table__.create(engine)
    try:
        yield sessionmaker(engine)
    finally:
        engine.dispose()


@pytest.fixture
def synthetic_file(tmp_path):
    path = tmp_path / "SYNTHETIC_TEST_ONLY.json"
    path.write_text(
        json.dumps(
            {
                "system": ICD10_AM_SYSTEM,
                "concepts": [
                    {
                        "code": "E11.9",
                        "display": "SYNTHETIC TEST ONLY - not a clinical description",
                        "active": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_missing_catalog(sessions):
    with sessions() as db:
        assert not diagnosis_catalog_readiness(db).present
        with pytest.raises(DiagnosisCatalogMissingError, match="catalog is not loaded"):
            find_term(db, "E11.9", (ICD10_AM_SYSTEM,))


def test_loaded_catalog_and_unknown_diagnosis(sessions, synthetic_file):
    assert (
        import_dataset(synthetic_file, dry_run=False, session_factory=sessions).inserted
        == 1
    )
    with sessions() as db:
        readiness = diagnosis_catalog_readiness(db)
        assert readiness.present and readiness.active_rows == 1
        assert find_term(db, "E11.9", (ICD10_AM_SYSTEM,)).code == "E11.9"
        assert find_term(db, "UNKNOWN-TEST-CODE", (ICD10_AM_SYSTEM,)) is None


def test_dry_run_no_write(sessions, synthetic_file):
    report = import_dataset(synthetic_file, session_factory=sessions)
    assert report.inserted == report.failed == 0
    assert report.would_insert == 1
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(NphiesTerminology)) == 0


def test_idempotent(sessions, synthetic_file):
    first = import_dataset(synthetic_file, dry_run=False, session_factory=sessions)
    second = import_dataset(synthetic_file, dry_run=False, session_factory=sessions)
    assert first.inserted == second.existing == 1
    assert second.inserted == second.failed == 0


@pytest.mark.parametrize(
    "mutation", ["system", "display", "active", "duplicate", "version"]
)
def test_invalid_file_never_opens_db(synthetic_file, mutation):
    data = json.loads(synthetic_file.read_text())
    if mutation == "system":
        data["system"] = "https://example.org/not-icd10-am"
    elif mutation == "display":
        del data["concepts"][0]["display"]
    elif mutation == "active":
        data["concepts"][0]["active"] = "true"
    elif mutation == "duplicate":
        data["concepts"] *= 2
    else:
        data["version"] = ""
    synthetic_file.write_text(json.dumps(data))

    def forbidden_connection():
        pytest.fail("Invalid dataset must not open a database session.")

    assert (
        import_dataset(synthetic_file, session_factory=forbidden_connection).failed == 1
    )


def test_inactive_catalog_is_present_but_diagnosis_rejected(sessions, synthetic_file):
    data = json.loads(synthetic_file.read_text())
    data["concepts"][0]["active"] = False
    synthetic_file.write_text(json.dumps(data))
    assert (
        import_dataset(synthetic_file, dry_run=False, session_factory=sessions).inserted
        == 1
    )
    with sessions() as db:
        assert diagnosis_catalog_readiness(db).present
        assert diagnosis_catalog_readiness(db).active_rows == 0
        assert find_term(db, "E11.9", (ICD10_AM_SYSTEM,)) is None


@pytest.mark.parametrize(
    "field,value",
    [("display", "Changed"), ("active", False)],
)
def test_existing_conflicts_are_not_overwritten(sessions, synthetic_file, field, value):
    import_dataset(synthetic_file, dry_run=False, session_factory=sessions)
    data = json.loads(synthetic_file.read_text())
    data["concepts"][0][field] = value
    synthetic_file.write_text(json.dumps(data))
    report = import_dataset(synthetic_file, dry_run=False, session_factory=sessions)
    assert report.failed == 1 and report.inserted == 0
    with sessions() as db:
        row = db.scalar(select(NphiesTerminology))
        assert row.is_active
        assert row.display == "SYNTHETIC TEST ONLY - not a clinical description"


@pytest.mark.parametrize("version", ["TEST-ONLY", None])
def test_version_metadata_is_dry_run_only(sessions, synthetic_file, version):
    data = json.loads(synthetic_file.read_text())
    data["version"] = version
    synthetic_file.write_text(json.dumps(data))
    assert load_approved_dataset(synthetic_file).version == version
    report = import_dataset(synthetic_file, session_factory=sessions)
    assert report.failed == report.inserted == 0
    assert report.would_insert == 1
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(NphiesTerminology)) == 0


@pytest.mark.parametrize("version", ["TEST-ONLY", None])
def test_version_metadata_apply_rejected_before_db(synthetic_file, version):
    data = json.loads(synthetic_file.read_text())
    data["version"] = version
    synthetic_file.write_text(json.dumps(data))

    def forbidden_connection():
        pytest.fail("Version-bearing apply must not open a database session.")

    report = import_dataset(
        synthetic_file, dry_run=False, session_factory=forbidden_connection
    )
    assert report.failed == 1
    assert report.inserted == report.existing == report.would_insert == 0
    assert "Version persistence is not yet supported" in report.diagnostic


def test_failure_does_not_report_insert_or_secret(sessions, synthetic_file, capsys):
    with sessions() as db:
        engine = db.get_bind()

    def fail(connection):
        raise RuntimeError("TEST_SECRET_DO_NOT_PRINT")

    event.listen(engine, "commit", fail)
    try:
        report = import_dataset(synthetic_file, dry_run=False, session_factory=sessions)
    finally:
        event.remove(engine, "commit", fail)
    report.print_summary(False)
    assert report.failed == 1 and report.inserted == 0
    assert "TEST_SECRET_DO_NOT_PRINT" not in capsys.readouterr().out
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(NphiesTerminology)) == 0


def test_schema_and_lookup_share_system_constant():
    from typing import get_args
    from diagnosis_systems import ICD10AMSystem
    from schemas.fhir_claim import DiagnosisCoding
    from services.terminology import DIAGNOSIS_SYSTEM

    assert get_args(ICD10AMSystem) == (ICD10_AM_SYSTEM,)
    assert DiagnosisCoding.model_fields["system"].annotation is ICD10AMSystem
    assert DIAGNOSIS_SYSTEM == ICD10_AM_SYSTEM


def test_malformed_file_is_safe(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text("{")
    report = import_dataset(path)
    assert report.failed == 1


def test_current_migration_schema_supports_importer(monkeypatch, synthetic_file):
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect
    import database

    engine = create_engine("sqlite://")
    monkeypatch.setattr(database, "engine", engine)
    try:
        config = Config("alembic.ini")
        assert (
            ScriptDirectory.from_config(config).get_current_head()
            == "0006_coverage_rule_history"
        )
        command.upgrade(config, "head")
        assert "version" not in {
            column["name"]
            for column in inspect(engine).get_columns("nphies_terminology")
        }
        factory = sessionmaker(engine)
        first = import_dataset(synthetic_file, dry_run=False, session_factory=factory)
        second = import_dataset(synthetic_file, dry_run=False, session_factory=factory)
        assert first.inserted == second.existing == 1
        assert first.failed == second.failed == second.inserted == 0
        with factory() as db:
            assert diagnosis_catalog_readiness(db).present
            assert find_term(db, "E11.9", (ICD10_AM_SYSTEM,)) is not None
    finally:
        engine.dispose()
