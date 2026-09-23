"""Integration tests with real AsyncSession, isolated SQLite and real JWTs."""

from copy import deepcopy
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from sqlalchemy.exc import OperationalError

from main import app
from database import get_db
from async_database import get_async_db
from models import (
    Base,
    NphiesTerminology,
    Organization,
    InsuranceCompany,
    DiagnosisCode,
    ServiceCode,
    DiagnosisServiceRule,
    AuditLog,
)
from testing_auth import provider_headers
from test_fhir_claim import payload  # Shared synthetic FHIR fixture.

PATH = "/api/v1/claims/pre-validate"


@pytest.fixture
def context(tmp_path):
    path = tmp_path / "claims.sqlite"
    engine = create_engine(
        "sqlite:///" + path.as_posix(), connect_args={"check_same_thread": False}
    )
    async_engine = create_async_engine(
        "sqlite+aiosqlite:///" + path.as_posix(), poolclass=NullPool
    )
    for target in (engine, async_engine.sync_engine):

        @event.listens_for(target, "connect")
        def enable_fk(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    async_sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    with sessions() as db:
        db.add(
            Organization(
                id=8,
                fhir_id="insurer-1",
                identifier_system="https://example.org/ids",
                identifier_value="test-1",
                name="Test insurer",
                organization_type="insurer",
            )
        )
        db.flush()
        db.add_all(
            [
                InsuranceCompany(id=42, name="Test insurer", organization_id=8),
                InsuranceCompany(id=99, name="Other insurer"),
                DiagnosisCode(id=1, code="E11.9", description="Test diagnosis"),
                DiagnosisCode(id=2, code="K35.80", description="Second diagnosis"),
                ServiceCode(id=1, code="83600-00-10", description="Test service"),
                ServiceCode(id=2, code="30571-00-00", description="Second service"),
            ]
        )
        db.commit()
        db.add_all(
            [
                NphiesTerminology(
                    code_system_url="http://hl7.org/fhir/sid/icd-10-am", code="E11.9"
                ),
                NphiesTerminology(
                    code_system_url="http://hl7.org/fhir/sid/icd-10-am", code="K35.80"
                ),
                NphiesTerminology(
                    code_system_url="http://nphies.sa/terminology/CodeSystem/services",
                    code="83600-00-10",
                ),
                NphiesTerminology(
                    code_system_url="http://nphies.sa/terminology/CodeSystem/services",
                    code="30571-00-00",
                ),
            ]
        )
        db.commit()
        headers = provider_headers(db)

    def sync_dependency():
        with sessions() as db:
            yield db

    async def async_dependency():
        async with async_sessions() as db:
            yield db

    original = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = sync_dependency
    app.dependency_overrides[get_async_db] = async_dependency
    try:
        with TestClient(app, headers=headers) as client:
            yield client, sessions
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)
        asyncio.run(async_engine.dispose())
        engine.dispose()


def rule(sessions, covered, insurer=None, diagnosis=1, service=1, deleted=False):
    with sessions() as db:
        db.add(
            DiagnosisServiceRule(
                diagnosis_id=diagnosis,
                service_id=service,
                insurer_id=insurer,
                is_covered=covered,
                is_deleted=deleted,
            )
        )
        db.commit()


def outcome(response, status):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/fhir+json")
    body = response.json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"]
    assert all(
        "severity" in issue and "code" in issue and "diagnostics" in issue
        for issue in body["issue"]
    )
    return body["issue"]


@pytest.mark.parametrize(
    "specific,global_rule,expected",
    [
        (True, False, 200),
        (False, True, 422),
        (None, True, 200),
        (None, False, 422),
        (None, None, 422),
    ],
)
def test_precedence(context, payload, specific, global_rule, expected):
    client, sessions = context
    if global_rule is not None:
        rule(sessions, global_rule)
    if specific is not None:
        rule(sessions, specific, insurer=42)
    issues = outcome(client.post(PATH, json=payload), expected)
    if expected == 200:
        assert issues[0]["severity"] == "information"
        assert issues[0]["code"] == "informational"
    else:
        assert issues[0]["details"]["coding"][0]["code"] in {
            "medical_necessity",
            "no_coverage_rule",
        }
        assert issues[0]["expression"]
    with sessions() as db:
        events = db.scalars(select(AuditLog)).all()
        assert len(events) == 1
        assert events[0].http_status == expected
        assert events[0].actor_user_id is not None


@pytest.mark.parametrize("scope", [None, 42])
@pytest.mark.parametrize("order", [(True, False), (False, True)])
def test_denial_wins_conflict(context, payload, scope, order):
    client, sessions = context
    for value in order:
        rule(sessions, value, insurer=scope)
    outcome(client.post(PATH, json=payload), 422)


def test_other_insurer_rule_cannot_override_global(context, payload):
    client, sessions = context
    rule(sessions, True)
    rule(sessions, False, insurer=99)
    outcome(client.post(PATH, json=payload), 200)


def test_soft_deleted_override_ignored(context, payload):
    client, sessions = context
    rule(sessions, True)
    rule(sessions, False, insurer=42, deleted=True)
    outcome(client.post(PATH, json=payload), 200)


@pytest.mark.parametrize(
    "target", ["organization", "insurance", "diagnosis", "service"]
)
def test_deleted_master_fails_closed(context, payload, target):
    client, sessions = context
    rule(sessions, True)
    model, key = {
        "organization": (Organization, 8),
        "insurance": (InsuranceCompany, 42),
        "diagnosis": (DiagnosisCode, 1),
        "service": (ServiceCode, 1),
    }[target]
    with sessions() as db:
        db.get(model, key).soft_delete()
        db.commit()
    outcome(client.post(PATH, json=payload), 422)


def test_identity_cannot_select_global_by_unknown_insurer(context, payload):
    client, sessions = context
    rule(sessions, True)
    payload["insurer"]["identifier"][0]["value"] = "unregistered"
    issues = outcome(client.post(PATH, json=payload), 422)
    assert issues[0]["code"] == "business-rule"


def test_collects_rejections_for_all_linked_pairs(context, payload):
    client, sessions = context
    second = deepcopy(payload["claim"]["diagnosis"][0])
    second["sequence"] = 2
    second["type"][0]["coding"][0]["code"] = "secondary"
    second["diagnosisCodeableConcept"]["coding"][0]["code"] = "K35.80"
    payload["claim"]["diagnosis"].append(second)
    payload["claim"]["item"][0]["diagnosisSequence"] = [1, 2]
    item = deepcopy(payload["claim"]["item"][0])
    item["sequence"] = 2
    item["productOrService"]["coding"][0]["code"] = "30571-00-00"
    payload["claim"]["item"].append(item)
    payload["claim"]["total"]["value"] = "414.00"
    rule(sessions, True, diagnosis=1, service=1)
    issues = outcome(client.post(PATH, json=payload), 422)
    assert len(issues) == 3
    assert all(
        issue["details"]["coding"][0]["code"] == "no_coverage_rule" for issue in issues
    )
    assert any("sequence = 2" in issue["expression"][1] for issue in issues)


def test_ambiguous_codings_rejected(context, payload):
    client, sessions = context
    rule(sessions, True)
    payload["claim"]["item"][0]["productOrService"]["coding"] *= 2
    issues = outcome(client.post(PATH, json=payload), 422)
    assert issues[0]["details"]["coding"][0]["code"] == "invalid_service"


@pytest.mark.parametrize("failure", ["total", "reference", "decimal", "missing"])
def test_pydantic_rejects_before_business_query(context, payload, failure):
    client, sessions = context
    if failure == "total":
        payload["claim"]["total"]["value"] = "207.01"
    elif failure == "reference":
        payload["claim"]["item"][0]["diagnosisSequence"] = [99]
    elif failure == "decimal":
        payload["claim"]["item"][0]["unitPrice"]["value"] = 100.1
    else:
        del payload["patient"]
    outcome(client.post(PATH, json=payload), 422)
    with sessions() as db:
        assert db.scalar(select(AuditLog)).reason == (
            "financial_invariant" if failure == "total" else "invalid_request"
        )


def test_requires_bearer(context, payload):
    client, _ = context
    client.headers.pop("Authorization")
    outcome(client.post(PATH, json=payload), 401)


def test_database_failure_is_sanitized(context, payload, monkeypatch):
    import claim_router

    async def fail(*args):
        raise OperationalError("secret query", {}, Exception("database password"))

    monkeypatch.setattr(claim_router, "validate_coverage", fail)
    client, _ = context
    response = client.post(PATH, json=payload)
    outcome(response, 503)
    assert "password" not in response.text
    assert "secret query" not in response.text


def test_openapi_contract(context):
    client, _ = context
    operation = client.get("/openapi.json").json()["paths"][PATH]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/ClaimSubmission")
    assert operation["security"]
    assert "application/fhir+json" in operation["responses"]["200"]["content"]


@pytest.mark.parametrize("target", ["service", "diagnosis"])
def test_unknown_codes(context, payload, target):
    client, sessions = context
    rule(sessions, True)
    if target == "service":
        payload["claim"]["item"][0]["productOrService"]["coding"][0]["code"] = "unknown"
    else:
        payload["claim"]["diagnosis"][0]["diagnosisCodeableConcept"]["coding"][0][
            "code"
        ] = "Z99.9"
    issues = outcome(client.post(PATH, json=payload), 422)
    assert issues[0]["code"] == "business-rule"


def test_role_rejection(context, payload):
    from auth import claim_access, require_roles

    client, _ = context
    # Tighten this route to admin for this test; the real provider JWT is denied.
    app.dependency_overrides[claim_access] = require_roles("admin")
    try:
        outcome(client.post(PATH, json=payload), 403)
    finally:
        del app.dependency_overrides[claim_access]


def test_numeric_factor_and_insurer_array(context, payload):
    client, sessions = context
    rule(sessions, True)
    payload["claim"]["item"][0]["factor"] = 0.9
    payload["insurer"] = [payload["insurer"]]
    outcome(client.post(PATH, json=payload), 200)


def test_default_factor_and_tax(context, payload):
    client, sessions = context
    rule(sessions, True)
    item = payload["claim"]["item"][0]
    del item["factor"]
    del item["extension"]
    item["net"]["value"] = "200.00"
    payload["claim"]["total"]["value"] = "200.00"
    outcome(client.post(PATH, json=payload), 200)


def test_financial_invariant(context, payload):
    client, sessions = context
    rule(sessions, True)
    payload["claim"]["item"][0]["net"]["value"] = "207.01"
    payload["claim"]["total"]["value"] = "207.01"
    issues = outcome(client.post(PATH, json=payload), 422)
    assert issues[0]["code"] == "invariant"


@pytest.mark.parametrize("kind", ["diagnosis", "service"])
def test_inactive_term_rejects_covered_pair(context, payload, kind):
    client, sessions = context
    rule(sessions, True)
    with sessions() as db:
        term = db.scalar(
            select(NphiesTerminology).where(
                NphiesTerminology.code
                == ("E11.9" if kind == "diagnosis" else "83600-00-10")
            )
        )
        term.is_active = False
        db.commit()
    issues = outcome(client.post(PATH, json=payload), 422)
    assert issues[0]["diagnostics"] == (
        "Diagnosis code is not active"
        if kind == "diagnosis"
        else "Service code is not active"
    )


def test_principal_not_first(context, payload):
    client, sessions = context
    rule(sessions, True)
    secondary = deepcopy(payload["claim"]["diagnosis"][0])
    secondary["sequence"] = 2
    secondary["type"][0]["coding"][0]["code"] = "secondary"
    secondary["diagnosisCodeableConcept"]["coding"][0]["code"] = "K35.80"
    payload["claim"]["diagnosis"].insert(0, secondary)
    outcome(client.post(PATH, json=payload), 200)


def test_unresolved_insurer_array(context, payload):
    client, _ = context
    payload["insurer"] = [payload["insurer"]]
    payload["insurer"][0]["id"] = "unrelated"
    outcome(client.post(PATH, json=payload), 422)


def test_tax_found_after_other_extension(context, payload):
    client, sessions = context
    rule(sessions, True)
    payload["claim"]["item"][0]["extension"].insert(
        0,
        {
            "url": "http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition/extension-package",
            "valueBoolean": False,
        },
    )
    outcome(client.post(PATH, json=payload), 200)


def test_duplicate_tax_rejected(context, payload):
    client, _ = context
    payload["claim"]["item"][0]["extension"] *= 2
    issues = outcome(client.post(PATH, json=payload), 422)
    assert issues[0]["code"] == "invariant"


def test_missing_principal_rejected(context, payload):
    client, sessions = context
    rule(sessions, True)
    payload["claim"]["diagnosis"][0]["type"][0]["coding"][0]["code"] = "secondary"
    issues = outcome(client.post(PATH, json=payload), 422)
    assert issues[0]["details"]["coding"][0]["code"] == "invalid_diagnosis"


@pytest.mark.parametrize("rejected_index", [1, 2])
def test_later_item_insurer_denial_cannot_hide_behind_first_approval(
    context, payload, rejected_index
):
    client, sessions = context
    first = payload["claim"]["item"][0]
    payload["claim"]["item"] = [deepcopy(first) for _ in range(3)]
    for index, item in enumerate(payload["claim"]["item"]):
        item["sequence"] = index + 1
    payload["claim"]["item"][rejected_index]["productOrService"]["coding"][0][
        "code"
    ] = "30571-00-00"
    payload["claim"]["total"]["value"] = "621.00"
    rule(sessions, True, service=1)
    rule(sessions, True, service=2)
    rule(sessions, False, insurer=42, service=2)
    issues = outcome(client.post(PATH, json=payload), 422)
    assert len(issues) == 1
    assert issues[0]["details"]["coding"][0]["code"] == "medical_necessity"
    assert f"item {rejected_index + 1}" in issues[0]["diagnostics"]


def test_audit_helper_can_perform_io_inside_greenlet_bridge(
    context, payload, monkeypatch
):
    import claim_router

    original = claim_router.add_event

    def audit_with_query(db, *args, **kwargs):
        assert (
            db.scalar(select(InsuranceCompany.id).where(InsuranceCompany.id == 42))
            == 42
        )
        original(db, *args, **kwargs)

    monkeypatch.setattr(claim_router, "add_event", audit_with_query)
    client, sessions = context
    rule(sessions, True)
    outcome(client.post(PATH, json=payload), 200)


def test_factor_decimal_exactness_and_one_halala_difference(context, payload):
    from decimal import Decimal
    from schemas.fhir_claim import ClaimSubmission

    client, sessions = context
    rule(sessions, True)
    item = payload["claim"]["item"][0]
    item["factor"] = 0.9
    item["quantity"]["value"] = "1"
    item["unitPrice"]["value"] = "333.30"
    item["extension"] = []
    item["net"]["value"] = "299.97"
    payload["claim"]["total"]["value"] = "299.97"
    model = ClaimSubmission.model_validate(payload)
    assert isinstance(model.claim.item[0].factor, Decimal)
    assert model.claim.item[0].factor == Decimal("0.9")
    assert model.claim.item[0].financials().total == Decimal("299.97")
    outcome(client.post(PATH, json=payload), 200)
    item["net"]["value"] = "299.98"
    payload["claim"]["total"]["value"] = "299.98"
    assert outcome(client.post(PATH, json=payload), 422)[0]["code"] == "invariant"
