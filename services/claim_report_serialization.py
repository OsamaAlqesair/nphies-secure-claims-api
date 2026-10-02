"""Versioned historical facts from the evaluated report, with no database reads."""

from services.claim_validation import report_issues
from services.claim_validation_report import ClaimValidationReport
from schemas.claim import Issue, OperationOutcome

REPORT_VERSION = "claim-validation-report-v1"


def serialize_report(report: ClaimValidationReport) -> dict:
    def insurer(value):
        return {
            "reference": value.reference,
            "fhir_id": value.fhir_id,
            "identifiers": [list(entry) for entry in value.identifiers],
            "insurance_company_id": value.insurance_company_id,
        }

    def term(value):
        return (
            {"code": value.code, "system": value.system, "display": value.display}
            if value is not None
            else None
        )

    observations = []
    for entry in report.observations:
        result = entry.business_result
        decision = entry.coverage
        observations.append(
            {
                "item_sequence": entry.item_sequence,
                "diagnosis_sequence": entry.diagnosis_sequence,
                "diagnosis_system": entry.diagnosis_system,
                "diagnosis_code": entry.diagnosis_code,
                "service_code": entry.service_code,
                "service_system": entry.service_system,
                "insurer": insurer(entry.insurer),
                "terminology_status": entry.terminology_status.value,
                "reason": entry.reason,
                "business_result": {
                    "pair": {
                        "diagnosis_code": result.pair.diagnosis_code,
                        "service_code": result.pair.service_code,
                        "service_system": result.pair.service_system,
                        "insurer_id": result.pair.insurer_id,
                    },
                    "diagnosis": term(result.diagnosis),
                    "service": term(result.service),
                    "reason": result.reason,
                    "is_valid": result.is_valid,
                    "coverage": (
                        {
                            "status": decision.status.value,
                            "diagnosis_id": decision.diagnosis_id,
                            "service_id": decision.service_id,
                            "matched_rule_ids": list(decision.matched_rule_ids),
                            "selected_scope": decision.selected_scope.value,
                            "insurer_id": decision.insurer_id,
                            "is_covered": decision.is_covered,
                        }
                        if decision is not None
                        else None
                    ),
                },
            }
        )
    termination = report.termination
    return {
        "schema_version": REPORT_VERSION,
        "result": report.result.value,
        "insurer": insurer(report.insurer),
        "observations": observations,
        "findings": [
            {
                "reason": entry.reason,
                "diagnostics": entry.diagnostics,
                "expression": (
                    list(entry.expression) if entry.expression is not None else None
                ),
                "code": entry.code,
            }
            for entry in report.findings
        ],
        "skipped_item_sequences": list(report.skipped_item_sequences),
        "termination": (
            {
                "stage": termination.stage,
                "reason": termination.reason,
                "item_sequence": termination.item_sequence,
                "diagnosis_sequence": termination.diagnosis_sequence,
            }
            if termination is not None
            else None
        ),
    }


def report_outcome(report: ClaimValidationReport) -> OperationOutcome:
    issues = report_issues(report)
    if not issues:
        issues = [
            Issue(
                severity="information",
                code="informational",
                diagnostics="Structural, financial and coverage pre-validation passed. This is not payer authorization.",
            )
        ]
    return OperationOutcome(issue=issues)
