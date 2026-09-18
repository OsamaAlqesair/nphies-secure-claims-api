"""Warehouse schema, soft-delete, migration and legacy preservation checks."""

from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path
import hashlib
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event, select, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import database
from models import (
    Base,
    Patient,
    Organization,
    Practitioner,
    Coverage,
    Encounter,
    Claim,
    DiagnosisCode,
    ServiceCode,
    DiagnosisServiceRule,
)
from services.coverage import resolve_coverage
from migrate_sqlite import import_legacy


@pytest.fixture
def warehouse():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def enable_fk(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def entities(session):
    provider = Organization(
        fhir_id="hospital",
        identifier_system="test",
        identifier_value="hospital",
        name="Hospital",
        organization_type="provider",
    )
    insurer = Organization(
        fhir_id="payer",
        identifier_system="test",
        identifier_value="payer",
        name="Payer",
        organization_type="insurer",
    )
    patient = Patient(
        fhir_id="patient",
        identifier_system="test",
        identifier_value="patient",
        name="Synthetic patient",
    )
    practitioner = Practitioner(
        fhir_id="doctor",
        identifier_system="test",
        identifier_value="doctor",
        name="Synthetic doctor",
        organization=provider,
    )
    coverage = Coverage(
        fhir_id="coverage",
        beneficiary=patient,
        payor=insurer,
        status="active",
        policy_number="TEST",
    )
    encounter = Encounter(
        fhir_id="visit",
        patient=patient,
        service_provider=provider,
        status="finished",
        encounter_class="AMB",
    )
    claim = Claim(
        fhir_id="claim",
        patient=patient,
        provider=provider,
        insurer=insurer,
        practitioner=practitioner,
        coverage=coverage,
        encounter=encounter,
        status="active",
        total=Decimal("0.30"),
    )
    session.add(claim)
    session.commit()
    return claim


def test_all_entities_have_nonnullable_audit_columns():
    for table in Base.metadata.tables.values():
        for column in ("is_deleted", "created_at", "updated_at"):
            assert not table.c[column].nullable
            assert table.c[column].server_default is not None


def test_relationships_and_exact_total_roundtrip(warehouse):
    with Session(warehouse) as session:
        claim = entities(session)
        session.expire_all()
        assert claim.total == Decimal("0.30")
        assert claim.patient is claim.coverage.beneficiary
        assert claim.patient is claim.encounter.patient
        assert claim.insurer is claim.coverage.payor
        assert claim.provider is claim.practitioner.organization
        assert claim in claim.patient.claims
        assert claim in claim.coverage.claims
        assert claim.created_at and claim.updated_at and not claim.is_deleted


@pytest.mark.parametrize("relationship", ["coverage", "encounter"])
def test_cross_patient_references_are_rejected(warehouse, relationship):
    with Session(warehouse) as session:
        claim = entities(session)
        other = Patient(
            fhir_id="other",
            identifier_system="test",
            identifier_value="other",
            name="Other",
        )
        session.add(other)
        session.flush()
        if relationship == "coverage":
            claim.coverage.beneficiary = other
        else:
            claim.encounter.patient = other
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_wrong_payor_rejected(warehouse):
    with Session(warehouse) as session:
        claim = entities(session)
        claim.insurer = claim.provider
        with pytest.raises(IntegrityError):
            session.commit()


def test_soft_delete_preserves_row_and_can_restore(warehouse):
    with Session(warehouse) as session:
        row = DiagnosisCode(code="TEST", description="Original")
        session.add(row)
        session.commit()
        created = row.created_at
        row.updated_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        session.commit()
        session.delete(row)
        session.commit()
        session.expunge_all()
        assert session.scalar(select(DiagnosisCode)) is None
        stored = session.scalar(
            select(DiagnosisCode).execution_options(include_deleted=True)
        )
        assert stored.is_deleted and stored.created_at == created
        assert stored.updated_at.year > 2000
        stored.restore()
        session.commit()
        assert session.scalar(select(DiagnosisCode)) is stored


def test_bulk_physical_delete_blocked(warehouse):
    with Session(warehouse) as session:
        with pytest.raises(ValueError, match="Physical ORM bulk deletes"):
            session.execute(delete(Patient))


def test_deleted_rules_cannot_approve_claim(warehouse):
    with Session(warehouse) as session:
        diagnosis = DiagnosisCode(code="G43", description="Test")
        service = ServiceCode(code="70450", description="Test")
        rule = DiagnosisServiceRule(
            diagnosis=diagnosis, service=service, is_covered=True
        )
        session.add(rule)
        session.commit()
        assert resolve_coverage(session, diagnosis.id, service.id, None).is_covered
        rule.soft_delete()
        session.commit()
        assert resolve_coverage(session, diagnosis.id, service.id, None) is None


def test_initial_migration_matches_models(monkeypatch):
    target = create_engine("sqlite://")
    monkeypatch.setattr(database, "engine", target)
    try:
        command.upgrade(Config("alembic.ini"), "head")
        with target.connect() as connection:
            assert (
                compare_metadata(MigrationContext.configure(connection), Base.metadata)
                == []
            )
    finally:
        target.dispose()


def test_postgres_migration_compiles_with_timestamp_triggers():
    output = StringIO()
    config = Config("alembic.ini", output_buffer=output)
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "JSONB" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "fk_claim_coverage_parties" in sql
    assert sql.count("EXECUTE FUNCTION nphies_audit_update()") == len(
        Base.metadata.tables
    )


def test_sqlite_import_preserves_ids_and_source(warehouse, tmp_path):
    source = tmp_path / "legacy.db"
    with sqlite3.connect(source) as legacy:
        legacy.executescript("""
        CREATE TABLE diagnosis_codes(id INTEGER PRIMARY KEY, code TEXT, description TEXT);
        CREATE TABLE service_codes(id INTEGER PRIMARY KEY, code TEXT, description TEXT);
        CREATE TABLE insurance_companies(id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE diagnosis_service_rules(id INTEGER PRIMARY KEY, diagnosis_id INTEGER, service_id INTEGER, insurer_id INTEGER, is_covered BOOLEAN);
        INSERT INTO diagnosis_codes VALUES(7,'G43','Test');
        INSERT INTO service_codes VALUES(8,'70450','Test');
        INSERT INTO insurance_companies VALUES(9,'Test insurer');
        INSERT INTO diagnosis_service_rules VALUES(10,7,8,NULL,1);
        """)
    digest = hashlib.sha256(source.read_bytes()).digest()
    assert import_legacy(source, warehouse)["diagnosis_service_rules"] == 1
    assert hashlib.sha256(source.read_bytes()).digest() == digest
    with Session(warehouse) as session:
        row = session.get(DiagnosisServiceRule, 10)
        assert row.diagnosis_id == 7 and row.service_id == 8 and row.insurer_id is None
        assert row.is_covered and not row.is_deleted and row.created_at
    with pytest.raises(RuntimeError, match="not empty"):
        import_legacy(source, warehouse)


def test_seed_is_idempotent_and_preserves_denials(warehouse, monkeypatch):
    import seed
    from sqlalchemy.orm import sessionmaker

    sessions = sessionmaker(bind=warehouse)
    monkeypatch.setattr(seed, "SessionLocal", sessions)
    seed.seed_data()
    with sessions() as session:
        rules = session.scalars(select(DiagnosisServiceRule)).all()
        assert len(rules) == 3
        for rule in rules:
            rule.is_covered = False
        session.commit()
    seed.seed_data()
    with sessions() as session:
        rules = session.scalars(select(DiagnosisServiceRule)).all()
        assert len(rules) == 3 and all(not rule.is_covered for rule in rules)
