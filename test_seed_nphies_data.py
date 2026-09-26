"""Seed reporting tests: temporary JSON and isolated in-memory SQLite only."""

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, event
from sqlalchemy.orm import sessionmaker

from models import NphiesTerminology
from seed_nphies_data import SOURCE_DIR, seed_terminologies
import seed_nphies_data


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    NphiesTerminology.__table__.create(engine)
    try:
        yield sessionmaker(engine, autoflush=False)
    finally:
        engine.dispose()


def source(path, codes=("A", "B")):
    (path / "codes.json").write_text(
        json.dumps(
            {
                "resourceType": "CodeSystem",
                "url": "https://example.org/test-system",
                "concept": [{"code": code, "display": code} for code in codes],
            }
        ),
        encoding="utf-8",
    )


def count(sessions):
    with sessions() as db:
        return db.scalar(select(func.count()).select_from(NphiesTerminology))


def test_new_records_count_after_commit(tmp_path, sessions):
    source(tmp_path)
    report = seed_terminologies(tmp_path, sessions)
    assert report.inserted == count(sessions) == 2
    assert report.existing == report.failed == 0
    assert (
        report.code_system_files_discovered == report.code_system_files_processed == 1
    )
    assert report.concepts_discovered == 2


def test_all_existing_and_second_run_no_duplicates(tmp_path, sessions, capsys):
    source(tmp_path)
    seed_terminologies(tmp_path, sessions)
    report = seed_terminologies(tmp_path, sessions)
    assert report.inserted == 0
    assert report.existing == 2
    assert count(sessions) == 2
    assert "all eligible concepts already exist" in capsys.readouterr().out


def test_malformed_json_counts_failure(tmp_path, sessions, capsys):
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    report = seed_terminologies(tmp_path, sessions)
    assert report.failed == report.file_errors == 1
    assert count(sessions) == 0
    output = capsys.readouterr().out
    assert "Completed with failures." in output
    assert "Success" not in output


@pytest.mark.parametrize("kind", ["missing", "file", "empty"])
def test_invalid_source(tmp_path, sessions, kind, capsys):
    path = tmp_path / "source"
    if kind == "file":
        path.write_text("not a directory", encoding="utf-8")
    elif kind == "empty":
        path.mkdir()
    report = seed_terminologies(path, sessions)
    assert report.failed == 1
    assert count(sessions) == 0
    assert "Source directory" in capsys.readouterr().out


def test_default_source_is_script_relative(tmp_path, sessions, monkeypatch):
    assert (
        SOURCE_DIR == Path(seed_nphies_data.__file__).resolve().parent / "nphies_data"
    )
    source(tmp_path)
    monkeypatch.setattr(seed_nphies_data, "SOURCE_DIR", tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert seed_terminologies(session_factory=sessions).inserted == 2


def test_failed_commit_does_not_count_inserts(tmp_path, sessions, capsys):
    source(tmp_path)
    with sessions() as session:
        engine = session.get_bind()

    def fail_commit(connection):
        raise RuntimeError("SECRET_DATABASE_CREDENTIAL")

    event.listen(engine, "commit", fail_commit)
    try:
        report = seed_terminologies(tmp_path, sessions)
    finally:
        event.remove(engine, "commit", fail_commit)
    assert report.inserted == count(sessions) == 0
    assert report.failed == 2
    assert report.file_errors == 1
    assert report.code_system_files_processed == 0
    assert "SECRET_DATABASE_CREDENTIAL" not in capsys.readouterr().out


def test_duplicate_concepts_and_missing_code_are_skipped(tmp_path, sessions):
    source(tmp_path, ("A", "A", ""))
    report = seed_terminologies(tmp_path, sessions)
    assert report.concepts_discovered == 3
    assert report.inserted == 1
    assert report.skipped == 2
    assert report.failed == 0


def test_valueset_not_imported(tmp_path, sessions):
    source(tmp_path)
    (tmp_path / "valueset.json").write_text(
        json.dumps(
            {
                "resourceType": "ValueSet",
                "compose": {
                    "include": [{"system": "http://hl7.org/fhir/sid/icd-10-am"}]
                },
            }
        ),
        encoding="utf-8",
    )
    report = seed_terminologies(tmp_path, sessions)
    assert report.ignored_files == 1
    assert report.inserted == 2


def test_deleted_row_is_not_duplicated_or_restored(tmp_path, sessions):
    source(tmp_path, ("A",))
    with sessions.begin() as db:
        db.add(
            NphiesTerminology(
                code_system_url="https://example.org/test-system",
                code="A",
                is_deleted=True,
                is_active=False,
            )
        )
    report = seed_terminologies(tmp_path, sessions)
    assert report.existing == 1
    assert report.inserted == 0
    with sessions() as db:
        rows = db.scalars(
            select(NphiesTerminology).execution_options(include_deleted=True)
        ).all()
        assert len(rows) == 1
        assert rows[0].is_deleted and not rows[0].is_active


def test_mixed_existing_and_new_rollback_counts(tmp_path, sessions):
    source(tmp_path)
    with sessions.begin() as db:
        db.add(
            NphiesTerminology(
                code_system_url="https://example.org/test-system", code="A"
            )
        )
    with sessions() as db:
        engine = db.get_bind()

    def fail_commit(connection):
        raise RuntimeError("commit rejected")

    event.listen(engine, "commit", fail_commit)
    try:
        report = seed_terminologies(tmp_path, sessions)
    finally:
        event.remove(engine, "commit", fail_commit)
    assert report.existing == 1
    assert report.inserted == 0
    assert report.failed == 1
    assert count(sessions) == 1


def test_sorted_files_and_duplicate_pair_across_files(tmp_path, sessions):
    source(tmp_path, ("A",))
    data = json.loads((tmp_path / "codes.json").read_text())
    data["concept"][0]["display"] = "First sorted file"
    (tmp_path / "a.json").write_text(json.dumps(data), encoding="utf-8")
    report = seed_terminologies(tmp_path, sessions)
    assert (
        report.code_system_files_discovered == report.code_system_files_processed == 2
    )
    assert report.inserted == report.existing == 1
    with sessions() as db:
        assert db.scalar(select(NphiesTerminology.display)) == "First sorted file"
