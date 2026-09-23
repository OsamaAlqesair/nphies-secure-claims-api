"""Pure schema tests: no live database or credentials required."""

from claim_router import router as claim_router
from copy import deepcopy
from decimal import Decimal, localcontext
import json

import pytest
from pydantic import ValidationError
from schemas.fhir_claim import ClaimSubmission, ClaimItem
from schemas.fhir_types import Money


def concept(system, code):
    return {"coding": [{"system": system, "code": code}]}


@pytest.fixture
def payload():
    identifier = [{"system": "https://example.org/ids", "value": "test-1"}]
    name = [{"family": "Test", "given": ["Patient"]}]
    item = {
        "sequence": 1,
        "diagnosisSequence": [1],
        "careTeamSequence": [1],
        "productOrService": concept(
            "http://nphies.sa/terminology/CodeSystem/services", "83600-00-10"
        ),
        "servicedDate": "2026-01-01",
        "quantity": {"value": "2"},
        "unitPrice": {"value": "100.00"},
        "factor": "0.9",
        "extension": [
            {
                "url": "http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition/extension-tax",
                "valueMoney": {"value": "27.00"},
            }
        ],
        "net": {"value": "207.00"},
    }
    return {
        "claim": {
            "id": "claim-1",
            "identifier": identifier,
            "status": "active",
            "type": concept(
                "http://terminology.hl7.org/CodeSystem/claim-type", "professional"
            ),
            "subType": concept(
                "http://nphies.sa/terminology/CodeSystem/claim-subtype", "op"
            ),
            "patient": {"reference": "Patient/patient-1"},
            "provider": {"reference": "Organization/provider-1"},
            "insurer": {"reference": "Organization/insurer-1"},
            "created": "2026-01-01T10:00:00+03:00",
            "priority": concept(
                "http://terminology.hl7.org/CodeSystem/processpriority", "normal"
            ),
            "careTeam": [
                {"sequence": 1, "provider": {"reference": "Practitioner/doctor-1"}}
            ],
            "insurance": [
                {
                    "sequence": 1,
                    "focal": True,
                    "coverage": {"reference": "Coverage/coverage-1"},
                }
            ],
            "diagnosis": [
                {
                    "sequence": 1,
                    "diagnosisCodeableConcept": concept(
                        "http://hl7.org/fhir/sid/icd-10-am", "E11.9"
                    ),
                    "type": [
                        concept(
                            "http://nphies.sa/terminology/CodeSystem/diagnosis-type",
                            "principal",
                        )
                    ],
                }
            ],
            "item": [item],
            "total": {"value": "207.00"},
        },
        "patient": {
            "id": "patient-1",
            "identifier": [
                {
                    "system": "http://nphies.sa/identifier/nationalid",
                    "value": "1000000001",
                }
            ],
            "name": name,
            "birthDate": "1990-01-01",
            "gender": "male",
        },
        "provider": {
            "id": "provider-1",
            "identifier": identifier,
            "name": "Test facility",
        },
        "insurer": {
            "id": "insurer-1",
            "identifier": identifier,
            "name": "Test insurer",
        },
        "practitioners": [{"id": "doctor-1", "identifier": identifier, "name": name}],
        "coverages": [
            {
                "id": "coverage-1",
                "identifier": identifier,
                "status": "active",
                "type": concept(
                    "http://terminology.hl7.org/CodeSystem/v3-ActCode", "EHCPOL"
                ),
                "beneficiary": {"reference": "Patient/patient-1"},
                "relationship": concept(
                    "http://terminology.hl7.org/CodeSystem/subscriber-relationship",
                    "self",
                ),
                "payor": [{"reference": "Organization/insurer-1"}],
            }
        ],
    }


def test_valid_and_exact_wire_round_trip(payload):
    submission = ClaimSubmission.model_validate(payload)
    breakdown = submission.claim.item[0].financials()
    assert (breakdown.gross, breakdown.net, breakdown.tax, breakdown.total) == (
        Decimal("200"),
        Decimal("180"),
        Decimal("27"),
        Decimal("207"),
    )
    wire = submission.to_fhir_json()
    parsed = json.loads(wire, parse_float=Decimal)
    assert parsed["claim"]["item"][0]["net"]["value"] == Decimal("207")
    assert not isinstance(parsed["claim"]["total"]["value"], str)
    assert "financials" not in parsed["claim"]["item"][0]
    assert ClaimSubmission.from_fhir_json(wire) == submission


@pytest.mark.parametrize(
    "value", [0.1, True, "-1", "NaN", "Infinity", "1.001", "1000000000000"]
)
def test_unsafe_money_rejected(value):
    with pytest.raises(ValidationError):
        Money(value=value)


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("patient", "birthDate", "2999-01-01"),
        ("patient", "birthDate", 0),
        ("claim", "created", "2026-01-01T10:00:00"),
        ("claim", "total", {"value": "207.01"}),
        ("claim", "patient", {"reference": "Patient/wrong"}),
        ("claim", "provider", {"reference": "Organization/wrong"}),
        ("claim", "unexpected", "reject"),
    ],
)
def test_invalid_submission(payload, section, field, value):
    payload[section][field] = value
    with pytest.raises(ValidationError):
        ClaimSubmission.model_validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("net", {"value": "180.00"}),
        ("diagnosisSequence", [99]),
        ("diagnosisSequence", [1, 1]),
        ("careTeamSequence", [99]),
        ("quantity", {"value": "0"}),
        ("unitPrice", {"value": "0.001"}),
    ],
)
def test_invalid_item(payload, field, value):
    payload["claim"]["item"][0][field] = value
    with pytest.raises(ValidationError):
        ClaimSubmission.model_validate(payload)


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "no_focal",
        "coverage",
        "payor",
        "beneficiary",
        "practitioner",
        "identity",
    ],
)
def test_relationships(payload, change):
    if change == "duplicate":
        payload["claim"]["diagnosis"] *= 2
    elif change == "no_focal":
        payload["claim"]["insurance"][0]["focal"] = False
    elif change == "coverage":
        payload["claim"]["insurance"][0]["coverage"]["reference"] = "Coverage/missing"
    elif change == "payor":
        payload["coverages"][0]["payor"][0]["reference"] = "Organization/wrong"
    elif change == "beneficiary":
        payload["coverages"][0]["beneficiary"]["reference"] = "Patient/wrong"
    elif change == "practitioner":
        payload["claim"]["careTeam"][0]["provider"][
            "reference"
        ] = "Practitioner/missing"
    else:
        payload["patient"]["identifier"][0]["value"] = "2000000001"
    with pytest.raises(ValidationError):
        ClaimSubmission.model_validate(payload)


def test_low_decimal_context(payload):
    with localcontext() as context:
        context.prec = 2
        assert ClaimSubmission.model_validate(payload).claim.total.value == Decimal(
            "207.00"
        )


def test_no_implicit_rounding(payload):
    item = deepcopy(payload["claim"]["item"][0])
    item.update(quantity={"value": "0.001"}, factor="1", unitPrice={"value": "0.01"})
    with pytest.raises(ValidationError):
        ClaimItem.model_validate(item)


def test_schema_can_be_generated():
    schema = ClaimSubmission.model_json_schema()
    assert "Claim" in schema["$defs"]


@pytest.mark.parametrize("code", ["e11.9", "E1.9", "E11.", "E11.999", "not-a-code"])
def test_icd_shape(payload, code):
    payload["claim"]["diagnosis"][0]["diagnosisCodeableConcept"]["coding"][0][
        "code"
    ] = code
    with pytest.raises(ValidationError):
        ClaimSubmission.model_validate(payload)


def test_currency(payload):
    payload["claim"]["item"][0]["unitPrice"]["currency"] = "USD"
    with pytest.raises(ValidationError):
        ClaimSubmission.model_validate(payload)


def test_cpt_intake(payload):
    payload["claim"]["item"][0]["productOrService"] = concept(
        "http://www.ama-assn.org/go/cpt", "70450"
    )
    assert (
        ClaimSubmission.model_validate(payload)
        .claim.item[0]
        .productOrService.coding[0]
        .code
        == "70450"
    )
