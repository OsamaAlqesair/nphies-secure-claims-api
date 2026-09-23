"""Terminology authority and legacy coverage foreign-key regression tests."""

import pytest
from sqlalchemy import select, create_engine
from models import NphiesTerminology, DiagnosisCode, ServiceCode
from services.terminology import DIAGNOSIS_SYSTEM, SERVICE_SYSTEMS
from test_main import database, client, payload, add_rule


@pytest.mark.parametrize("kind", ["diagnosis", "service"])
@pytest.mark.parametrize("state", ["inactive", "deleted", "wrong_system"])
def test_ineligible_terminology_rejects_legacy_known_code(
    client, database, payload, add_rule, kind, state
):
    add_rule(covered=True)
    with database() as db:
        term = db.get(NphiesTerminology, 101 if kind == "diagnosis" else 201)
        if state == "inactive":
            term.is_active = False
        elif state == "deleted":
            term.soft_delete()
        else:
            term.code_system_url = "http://nphies.sa/terminology/CodeSystem/claim-type"
        db.commit()
    response = client.post("/process-claim", json=payload)
    assert response.status_code == 400
    assert response.json()["resourceType"] == "OperationOutcome"
    assert response.headers["content-type"].startswith("application/fhir+json")


def test_terminology_ids_are_not_rule_foreign_keys(client, payload, add_rule):
    add_rule(covered=True)
    response = client.post("/process-claim", json=payload)
    assert response.status_code == 200
    assert "Official test service" in response.json()["message"]
    assert "Official test diagnosis" in response.json()["message"]


@pytest.mark.parametrize("kind", ["diagnosis", "service"])
def test_valid_terminology_without_rule_mapping_denies(client, database, payload, kind):
    with database() as db:
        db.add(
            NphiesTerminology(
                code="NEW",
                display="New official test entry",
                code_system_url=(
                    DIAGNOSIS_SYSTEM if kind == "diagnosis" else SERVICE_SYSTEMS[0]
                ),
            )
        )
        db.commit()
    payload[kind + "_code"] = "NEW"
    response = client.post("/process-claim", json=payload)
    assert response.status_code == 422
    assert "No applicable coverage rule" in response.json()["issue"][0]["diagnostics"]


@pytest.mark.parametrize("same_system", [True, False])
def test_ambiguous_code_denied(client, database, payload, add_rule, same_system):
    add_rule(covered=True)
    with database() as db:
        db.add(
            NphiesTerminology(
                code="70450",
                display="Duplicate",
                code_system_url=(
                    SERVICE_SYSTEMS[1] if same_system else SERVICE_SYSTEMS[0]
                ),
            )
        )
        db.commit()
    response = client.post("/process-claim", json=payload)
    assert response.status_code == 400
    assert response.json()["resourceType"] == "OperationOutcome"


@pytest.mark.parametrize("model", [DiagnosisCode, ServiceCode])
def test_deleted_rule_mapping_is_not_used(client, database, payload, add_rule, model):
    add_rule(covered=True)
    with database() as db:
        db.get(model, 1).soft_delete()
        db.commit()
    assert client.post("/process-claim", json=payload).status_code == 422


def test_migration_preserves_preseeded_table(monkeypatch):
    import database as database_module
    from alembic import command
    from alembic.config import Config

    engine = create_engine("sqlite://")
    monkeypatch.setattr(database_module, "engine", engine)
    try:
        command.upgrade(Config("alembic.ini"), "0003_audit_logs")
        NphiesTerminology.__table__.create(engine)
        with engine.begin() as connection:
            connection.execute(
                NphiesTerminology.__table__.insert().values(
                    id=900,
                    code_system_url=DIAGNOSIS_SYSTEM,
                    code="G43",
                    display="Preserved",
                )
            )
        command.upgrade(Config("alembic.ini"), "head")
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    select(NphiesTerminology.display).where(NphiesTerminology.id == 900)
                )
                == "Preserved"
            )
    finally:
        engine.dispose()
