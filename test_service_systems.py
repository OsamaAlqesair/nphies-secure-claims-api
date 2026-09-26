"""Service-system consistency; only an isolated in-memory database is used."""

from typing import get_args

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import NphiesTerminology
from schemas.fhir_claim import ServiceCoding
from service_systems import SERVICE_SYSTEMS, ServiceSystem
from services import terminology

CPT = "http://www.ama-assn.org/go/cpt"


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    NphiesTerminology.__table__.create(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


@pytest.mark.parametrize("system", SERVICE_SYSTEMS)
def test_every_supported_system_passes_schema(system):
    assert ServiceCoding(system=system, code="TEST").system == system


@pytest.mark.parametrize("system", [CPT, "https://example.org/unsupported"])
def test_unsupported_system_fails_schema(system):
    with pytest.raises(ValidationError):
        ServiceCoding(system=system, code="TEST")


def test_schema_and_lookup_share_definition():
    assert ServiceCoding.model_fields["system"].annotation is ServiceSystem
    assert terminology.SERVICE_SYSTEMS is SERVICE_SYSTEMS
    assert get_args(ServiceSystem) == SERVICE_SYSTEMS
    assert set(
        ServiceCoding.model_json_schema()["properties"]["system"]["enum"]
    ) == set(SERVICE_SYSTEMS)
    assert len(SERVICE_SYSTEMS) == 7
    assert CPT not in SERVICE_SYSTEMS


@pytest.mark.parametrize("system", SERVICE_SYSTEMS)
def test_lookup_accepts_each_supported_system(db, system):
    db.add(NphiesTerminology(code_system_url=system, code="TEST", display="Synthetic"))
    db.flush()
    term = terminology.find_term(db, "TEST", terminology.SERVICE_SYSTEMS)
    assert term is not None
    assert term.code_system_url == system


def test_cpt_excluded_from_lookup_even_if_row_exists(db):
    db.add(NphiesTerminology(code_system_url=CPT, code="70450", display="Synthetic"))
    db.flush()
    assert terminology.find_term(db, "70450", terminology.SERVICE_SYSTEMS) is None
