"""Boundary, financial, and rule precedence tests; never use rules_engine.db."""

from collections.abc import Iterator
from decimal import Decimal, localcontext
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from pydantic import ValidationError

from main import app, get_db
from testing_auth import provider_headers
from models import (
    Base,
    DiagnosisCode,
    NphiesTerminology,
    ServiceCode,
    InsuranceCompany,
    DiagnosisServiceRule,
)
from schemas.claim import ClaimPayload


@pytest.fixture
def database() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as db:
        db.add_all(
            [
                DiagnosisCode(id=1, code="G43", description="Test diagnosis"),
                DiagnosisCode(id=2, code="J00", description="Unrelated diagnosis"),
                ServiceCode(id=1, code="70450", description="Test service"),
                ServiceCode(id=2, code="OTHER", description="Unrelated service"),
                NphiesTerminology(
                    id=101,
                    code_system_url="http://hl7.org/fhir/sid/icd-10-am",
                    code="G43",
                    display="Official test diagnosis",
                ),
                NphiesTerminology(
                    id=102,
                    code_system_url="http://hl7.org/fhir/sid/icd-10-am",
                    code="J00",
                    display="Other diagnosis",
                ),
                NphiesTerminology(
                    id=201,
                    code_system_url="http://nphies.sa/terminology/CodeSystem/procedures",
                    code="70450",
                    display="Official test service",
                ),
                NphiesTerminology(
                    id=202,
                    code_system_url="http://nphies.sa/terminology/CodeSystem/services",
                    code="OTHER",
                    display="Other service",
                ),
                InsuranceCompany(id=1, name="Test insurer 1"),
                InsuranceCompany(id=2, name="Test insurer 2"),
            ]
        )
        db.commit()
    try:
        yield sessions
    finally:
        engine.dispose()


@pytest.fixture
def client(database) -> Iterator[TestClient]:
    def override():
        with database() as db:
            yield db

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override
    try:
        with database() as session:
            headers = provider_headers(session)
        with TestClient(app, headers=headers) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.fixture
def add_rule(database):
    def add(insurer_id=None, covered=True, diagnosis_id=1, service_id=1):
        with database() as db:
            db.add(
                DiagnosisServiceRule(
                    insurer_id=insurer_id,
                    is_covered=covered,
                    diagnosis_id=diagnosis_id,
                    service_id=service_id,
                )
            )
            db.commit()

    return add


@pytest.fixture
def payload() -> dict[str, Any]:
    return dict(
        diagnosis_code="G43",
        service_code="70450",
        insurer_id=1,
        billed_amount="500.00",
        allowed_amount="400.00",
        copay="50.00",
        net_payable="350.00",
    )


def assert_outcome(response, status=422, code=None):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].split(";")[0] == "application/fhir+json"
    body = response.json()
    assert body["resourceType"] == "OperationOutcome"
    assert "detail" not in body
    assert body["issue"]
    for issue in body["issue"]:
        assert issue["severity"] == "error"
        assert issue["code"] in {
            "invalid",
            "required",
            "structure",
            "invariant",
            "business-rule",
        }
        assert isinstance(issue["diagnostics"], str) and issue["diagnostics"]
        assert "input" not in issue and "ctx" not in issue
    if code:
        assert body["issue"][0]["code"] == code
    return body


@pytest.mark.parametrize(
    ("allowed", "copay", "net"),
    [
        ("400.00", "50.00", "350.00"),
        ("0.30", "0.10", "0.20"),
        ("100.01", "0.02", "99.99"),
        ("0.00", "0.00", "0.00"),
        ("20.00", "20.00", "0.00"),
        ("999999999999.99", "0.01", "999999999999.98"),
    ],
)
def test_exact_money_approval(client, add_rule, payload, allowed, copay, net):
    add_rule(1)
    payload.update(
        billed_amount=allowed, allowed_amount=allowed, copay=copay, net_payable=net
    )
    response = client.post("/process-claim", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
    assert response.json()["net_payable"] == net  # Money stays a string on output.


@pytest.mark.parametrize("net", ["349.99", "350.01", "400.00"])
def test_math_mismatch(client, add_rule, payload, net):
    add_rule(1)
    payload["net_payable"] = net
    assert_outcome(client.post("/process-claim", json=payload), code="invariant")


@pytest.mark.parametrize(
    "changes",
    [
        {"billed_amount": "399.99"},
        {"copay": "400.01", "net_payable": "0.00"},
    ],
)
def test_financial_bounds(client, payload, changes):
    payload.update(changes)
    assert_outcome(client.post("/process-claim", json=payload), code="invariant")


@pytest.mark.parametrize(
    "field", ["billed_amount", "allowed_amount", "copay", "net_payable"]
)
@pytest.mark.parametrize(
    "bad",
    [
        "-0.01",
        "NaN",
        "Infinity",
        "1.001",
        "1e2",
        "1000000000000.00",
        "",
        " 400.00 ",
        "1,000.00",
        True,
        None,
        400.0,
        [],
        {},
    ],
)
def test_invalid_money(client, payload, field, bad):
    payload[field] = bad
    assert_outcome(client.post("/process-claim", json=payload), code="invalid")


def test_integer_money_accepted(client, add_rule, payload):
    add_rule(1)
    payload.update(billed_amount=500, allowed_amount=400, copay=50, net_payable=350)
    response = client.post("/process-claim", json=payload)
    assert response.status_code == 200
    assert response.json()["net_payable"] == "350.00"


def test_pydantic_enforces_math_without_endpoint(payload):
    payload["net_payable"] = "350.01"
    with pytest.raises(ValidationError) as caught:
        ClaimPayload.model_validate(payload)
    assert caught.value.errors()[0]["type"] == "financial_invariant"


def test_financial_validation_ignores_decimal_context(payload):
    payload.update(
        billed_amount="999999999999.99",
        allowed_amount="999999999999.99",
        copay="0.01",
        net_payable="999999999999.98",
    )
    with localcontext() as context:
        context.prec = 6
        claim = ClaimPayload.model_validate(payload)
        assert claim.net_payable == Decimal("999999999999.98")


@pytest.mark.parametrize("specific", [True, False])
@pytest.mark.parametrize("global_covered", [True, False])
@pytest.mark.parametrize("reverse", [False, True])
def test_specific_rule_precedes_global(
    client, add_rule, payload, specific, global_covered, reverse
):
    rules = [(None, global_covered), (1, specific)]
    for insurer, covered in reversed(rules) if reverse else rules:
        add_rule(insurer, covered)
    response = client.post("/process-claim", json=payload)
    if specific:
        assert response.status_code == 200, response.text
    else:
        assert_outcome(response, code="business-rule")


@pytest.mark.parametrize("insurer", [None, 1, 2])
@pytest.mark.parametrize("covered", [True, False])
def test_global_fallback(client, add_rule, payload, insurer, covered):
    add_rule(None, covered)
    payload["insurer_id"] = insurer
    response = client.post("/process-claim", json=payload)
    if covered:
        assert response.status_code == 200
    else:
        assert_outcome(response, code="business-rule")


@pytest.mark.parametrize("scope", [None, 1])
@pytest.mark.parametrize("reverse", [False, True])
def test_denial_wins_conflicting_rules_in_same_scope(
    client, add_rule, payload, scope, reverse
):
    for covered in ([False, True] if reverse else [True, False]):
        add_rule(scope, covered)
    assert_outcome(client.post("/process-claim", json=payload), code="business-rule")


def test_other_insurer_does_not_override_fallback(client, add_rule, payload):
    add_rule(None, True)
    add_rule(2, False)
    assert client.post("/process-claim", json=payload).status_code == 200


@pytest.mark.parametrize("unrelated", [None, "insurer", "diagnosis", "service"])
def test_missing_applicable_rule_denies(client, add_rule, payload, unrelated):
    if unrelated == "insurer":
        add_rule(2, True)
    elif unrelated == "diagnosis":
        add_rule(None, True, diagnosis_id=2)
    elif unrelated == "service":
        add_rule(None, True, service_id=2)
    assert_outcome(client.post("/process-claim", json=payload), code="business-rule")


@pytest.mark.parametrize("field", ["diagnosis_code", "service_code"])
def test_unknown_master_code(client, payload, field):
    payload[field] = "UNKNOWN"
    assert_outcome(
        client.post("/process-claim", json=payload), status=400, code="business-rule"
    )


@pytest.mark.parametrize(
    "field",
    [
        "diagnosis_code",
        "service_code",
        "billed_amount",
        "allowed_amount",
        "copay",
        "net_payable",
    ],
)
def test_missing_required_fields(client, payload, field):
    del payload[field]
    assert_outcome(client.post("/process-claim", json=payload), code="required")


@pytest.mark.parametrize("insurer", [0, -1, True, 1.5, "1"])
def test_invalid_insurer(client, payload, insurer):
    payload["insurer_id"] = insurer
    assert_outcome(client.post("/process-claim", json=payload), code="invalid")


def test_omitted_insurer_uses_global(client, add_rule, payload):
    add_rule(None, True)
    del payload["insurer_id"]
    assert client.post("/process-claim", json=payload).status_code == 200


def test_unknown_field_does_not_leak_input(client, payload):
    payload["PRIVATE-SENTINEL"] = "SECRET-VALUE"
    response = client.post("/process-claim", json=payload)
    assert_outcome(response, code="structure")
    assert (
        "PRIVATE-SENTINEL" not in response.text and "SECRET-VALUE" not in response.text
    )


def test_malformed_json(client):
    assert_outcome(
        client.post(
            "/process-claim",
            content='{"secret":',
            headers={"Content-Type": "application/json"},
        ),
        code="structure",
    )


def test_openapi_describes_fhir_errors_and_decimal_strings(client):
    document = client.get("/openapi.json").json()
    for status in ["400", "422"]:
        content = document["paths"]["/process-claim"]["post"]["responses"][status][
            "content"
        ]
        assert "application/fhir+json" in content
        assert "application/json" not in content
    assert "HTTPValidationError" not in document["components"]["schemas"]
