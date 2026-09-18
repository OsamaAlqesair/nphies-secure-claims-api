"""Scalar integration and claim-rule regression tests using disposable SQLite."""

import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from main import SCALAR_JS_URL, app, get_db
from testing_auth import provider_headers
from models import (
    Base,
    DiagnosisCode,
    ServiceCode,
    InsuranceCompany,
    DiagnosisServiceRule,
)


def scalar_config(html):
    match = re.search(
        r'Scalar.createApiReference\("#app", (.*?)\)\s*</script>', html, re.S
    )
    assert match, "Scalar initialization missing"
    return json.loads(match.group(1))


def test_docs_use_pinned_bundle_and_current_openapi():
    with TestClient(app) as client:
        response = client.get("/docs")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert f'src="{SCALAR_JS_URL}"' in response.text
        assert "@1.68.0/dist/browser/standalone.js" in SCALAR_JS_URL
        config = scalar_config(response.text)
        assert "content" in config
        assert config["servers"] == [{"url": "http://testserver"}]
        assert not config.get("hideTestRequestButton", False)
        assert not config.get("proxyUrl")
        document = client.get("/openapi.json").json()
        assert config["content"] == document
        operation = document["paths"]["/process-claim"]["post"]
        assert operation["operationId"]
        assert operation["requestBody"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ClaimPayload"
        }
        assert "/docs" not in document["paths"]
        # The documented inputs are decimal strings or exact integers (never floats).
        assert {
            item["type"]
            for item in document["components"]["schemas"]["ClaimPayload"]["properties"][
                "allowed_amount"
            ]["anyOf"]
        } == {"string", "integer"}


def test_docs_follow_host_and_proxy_prefix():
    with TestClient(
        app, base_url="https://claims.example.test", root_path="/gateway"
    ) as client:
        config = scalar_config(client.get("/docs").text)
        assert "/process-claim" in config["content"]["paths"]
        assert config["servers"] == [{"url": "https://claims.example.test/gateway"}]


@pytest.fixture
def isolated_client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add_all(
            [
                DiagnosisCode(id=1, code="G43", description="Synthetic diagnosis"),
                ServiceCode(id=1, code="70450", description="Synthetic service"),
                InsuranceCompany(id=1, name="Synthetic insurer"),
                DiagnosisServiceRule(
                    diagnosis_id=1, service_id=1, insurer_id=None, is_covered=True
                ),
                DiagnosisServiceRule(
                    diagnosis_id=1, service_id=1, insurer_id=1, is_covered=False
                ),
            ]
        )
        db.commit()

    def override_db():
        with sessions() as db:
            yield db

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override_db
    try:
        with sessions() as session:
            headers = provider_headers(session)
        with TestClient(app, headers=headers) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        engine.dispose()


@pytest.mark.parametrize(
    ("changes", "expected", "diagnostic"),
    [
        ({}, 200, None),
        ({"insurer_id": 2}, 200, None),
        ({"insurer_id": 1}, 422, "Medical Necessity Denied"),
        ({"net_payable": "350.01"}, 422, "Financial math error"),
        ({"billed_amount": "399.00"}, 422, "Billed amount cannot be less"),
    ],
)
def test_claim_behavior_unchanged(isolated_client, changes, expected, diagnostic):
    payload = {
        "diagnosis_code": "G43",
        "service_code": "70450",
        "insurer_id": None,
        "billed_amount": "500.00",
        "allowed_amount": "400.00",
        "copay": "50.00",
        "net_payable": "350.00",
    }
    payload.update(changes)
    response = isolated_client.post("/process-claim", json=payload)
    assert response.status_code == expected, response.text
    result = response.json()
    if expected == 200:
        assert result["status"] == "approved"
    else:
        assert result["resourceType"] == "OperationOutcome"
        assert diagnostic in result["issue"][0]["diagnostics"]
