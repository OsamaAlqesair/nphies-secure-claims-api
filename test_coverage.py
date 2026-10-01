"""Structured coverage regression tests using isolated SQLite fixtures."""

import pytest
from sqlalchemy import select
from models import DiagnosisCode, ServiceCode, DiagnosisServiceRule
from services.coverage import (
    CoverageScope,
    CoverageStatus,
    resolve_coverage_by_codes,
)
from test_main import database, client, payload, add_rule
from testing_coverage import audited_rule, historical_rules
from services.coverage_mutations import soft_delete_rule, MutationContext


@pytest.mark.parametrize(
    "diagnosis,service,status,diagnosis_id,service_id",
    [
        ("absent", "70450", CoverageStatus.DIAGNOSIS_MAPPING_MISSING, None, 1),
        ("G43", "absent", CoverageStatus.SERVICE_MAPPING_MISSING, 1, None),
        ("absent", "absent", CoverageStatus.DIAGNOSIS_MAPPING_MISSING, None, None),
    ],
)
def test_missing_mapping_metadata(
    database, diagnosis, service, status, diagnosis_id, service_id
):
    with database() as db:
        result = resolve_coverage_by_codes(db, diagnosis, service, 1)
    assert result.status is status
    assert result.diagnosis_id == diagnosis_id
    assert result.service_id == service_id
    assert result.selected_scope is CoverageScope.NONE
    assert result.matched_rule_ids == ()
    assert result.insurer_id is None
    assert not result.is_covered


@pytest.mark.parametrize(
    "deleted_model,status",
    [
        (DiagnosisCode, CoverageStatus.DIAGNOSIS_MAPPING_MISSING),
        (ServiceCode, CoverageStatus.SERVICE_MAPPING_MISSING),
    ],
)
def test_deleted_mapping_is_missing(database, deleted_model, status):
    with database() as db:
        db.get(deleted_model, 1).soft_delete()
        db.commit()
        assert resolve_coverage_by_codes(db, "G43", "70450", 1).status is status


@pytest.mark.parametrize(
    "rules,requested,status,scope,database",
    [
        (
            *case,
            (
                "0008_coverage_writer_sources"
                if len({i for i, _ in case[0]}) != len(case[0])
                else "head"
            ),
        )
        for case in [
            ([], 1, CoverageStatus.NO_APPLICABLE_RULE, CoverageScope.NONE),
            ([(2, True)], 1, CoverageStatus.NO_APPLICABLE_RULE, CoverageScope.NONE),
            ([(None, True)], 1, CoverageStatus.APPROVED, CoverageScope.GLOBAL),
            ([(None, False)], 1, CoverageStatus.DENIED, CoverageScope.GLOBAL),
            (
                [(None, False), (1, True)],
                1,
                CoverageStatus.APPROVED,
                CoverageScope.INSURER_SPECIFIC,
            ),
            (
                [(None, True), (1, False)],
                1,
                CoverageStatus.DENIED,
                CoverageScope.INSURER_SPECIFIC,
            ),
            (
                [(None, True), (None, False)],
                1,
                CoverageStatus.DENIED,
                CoverageScope.GLOBAL,
            ),
            (
                [(1, True), (1, False), (None, True)],
                1,
                CoverageStatus.DENIED,
                CoverageScope.INSURER_SPECIFIC,
            ),
            (
                [(None, True), (2, False)],
                1,
                CoverageStatus.APPROVED,
                CoverageScope.GLOBAL,
            ),
            (
                [(None, True), (1, False)],
                None,
                CoverageStatus.APPROVED,
                CoverageScope.GLOBAL,
            ),
        ]
    ],
    indirect=["database"],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_resolution_metadata_and_precedence(
    database, rules, requested, status, scope, reverse
):
    with database() as db:
        selected_ids = []
        for insurer, covered in reversed(rules) if reverse else rules:
            row = DiagnosisServiceRule(
                diagnosis_id=1, service_id=1, insurer_id=insurer, is_covered=covered
            )
            if db.info.get("pre_identity"):
                historical_rules(db, row)
            else:
                row = audited_rule(db, row)
            if (scope is CoverageScope.GLOBAL and insurer is None) or (
                scope is CoverageScope.INSURER_SPECIFIC and insurer == requested
            ):
                selected_ids.append(row.id)
        db.commit()
        result = resolve_coverage_by_codes(db, "G43", "70450", requested)
    assert result.status is status
    assert result.is_covered is (status is CoverageStatus.APPROVED)
    assert (result.diagnosis_id, result.service_id) == (1, 1)
    assert result.matched_rule_ids == tuple(sorted(selected_ids))
    assert result.selected_scope is scope
    assert result.insurer_id == (
        requested if scope is CoverageScope.INSURER_SPECIFIC else None
    )


def test_deleted_rule_not_in_matched_metadata(database, add_rule):
    add_rule(covered=True)
    add_rule(1, False)
    with database() as db:
        override = db.scalar(
            select(DiagnosisServiceRule).where(DiagnosisServiceRule.insurer_id == 1)
        )
        soft_delete_rule(
            db,
            override.id,
            context=MutationContext(reason="Synthetic deleted override"),
        )
        db.commit()
        result = resolve_coverage_by_codes(db, "G43", "70450", 1)
        assert result.status is CoverageStatus.APPROVED
        assert result.selected_scope is CoverageScope.GLOBAL
        assert override.id not in result.matched_rule_ids


@pytest.mark.parametrize(
    "case,reason",
    [
        ("diagnosis", "diagnosis_mapping_missing"),
        ("service", "service_mapping_missing"),
        ("none", "no_coverage_rule"),
        ("denial", "medical_necessity"),
    ],
)
def test_legacy_public_failure_codes(client, database, payload, add_rule, case, reason):
    if case in {"diagnosis", "service"}:
        with database() as db:
            db.get(
                DiagnosisCode if case == "diagnosis" else ServiceCode, 1
            ).soft_delete()
            db.commit()
    elif case == "denial":
        add_rule(covered=False)
    response = client.post("/process-claim", json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["resourceType"] == "OperationOutcome"
    issue = body["issue"][0]
    assert issue["code"] == "business-rule"
    assert issue["details"]["coding"][0]["code"] == reason
    for internal in ("matched_rule_ids", "diagnosis_id", "service_id", "insurer_id"):
        assert internal not in response.text
