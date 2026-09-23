# FHIR Claim models

- `fhir_types.py`: shared identifiers, references, coding, exact decimals and Money.
- `fhir_resources.py`: Patient, Organization, Practitioner and Coverage.
- `fhir_claim.py`: Claim, diagnoses, items, insurance, care team and ClaimSubmission.
- `../test_fhir_claim.py`: complete synthetic intake example and validation tests.

## Usage

```python
from schemas.fhir_claim import ClaimSubmission

submission = ClaimSubmission.model_validate(payload)
breakdown = submission.claim.item[0].financials()
claim_json = submission.claim.to_fhir_json()
```

ClaimSubmission is an internal envelope, not a FHIR resource or message Bundle.
The existing /process-claim contract remains separate. POST
/api/v1/claims/pre-validate accepts ClaimSubmission, requires a provider/admin
Bearer token, and evaluates coverage using an injected AsyncSession.

For a conventional FastAPI JSON input, send monetary/quantity/factor values as
decimal strings. Binary floats are deliberately rejected. To read FHIR JSON
numeric decimals exactly, use ClaimSubmission.from_fhir_json(raw_body), or
Claim.from_fhir_json(raw_body) for a standalone Claim. Use to_fhir_json() for
FHIR output; ordinary Pydantic JSON serialization emits Decimal as strings.
A FastAPI adapter must bound request sizes and map errors to OperationOutcome.

## Financial meaning

Internal gross = quantity * unitPrice, before factor and tax.
Internal net = gross * factor, before tax.
Tax is read from the extension whose URL ends with extension-tax; absent tax is zero.
NPHIES item.net = internal net + tax.
Claim.total = sum(item.net).

No rounding or tax rate is inferred. Amounts with fractional halalas are rejected.
The chosen two-decimal policy is an application constraint. A future pricing
policy must explicitly define rounding when fractional quantities are permitted.

## Scope

These are strict, bounded FHIR-shaped subsets, not the complete FHIR R4 schema.
Unknown fields are rejected intentionally. Codes require authoritative,
version-aware terminology membership checks beyond regex validation.
CPT intake support does not establish acceptance by NPHIES.
Other service code systems, package/detail pricing, clinical supportingInfo and
additional profile extensions require explicit models before use.

Full NPHIES submission requires the selected Claim profile, required extensions,
profiled related resources including Encounter, MessageHeader and a message
Bundle, plus validation against the approved implementation-guide package.
No conformance profile is asserted by these models.

Sources:
- https://portal.nphies.sa/ig/usecases.html
- https://portal.nphies.sa/ig/usecase-claims.html
- https://portal.nphies.sa/ig/StructureDefinition-claim-base.html
- https://portal.nphies.sa/ig/CodeSystem-services.html

## Shared rules engine

The FHIR router now calls find_term and resolve_coverage_by_codes through an
AsyncSession.run_sync adapter. Each item checks the principal diagnosis and
all linked diagnoses. Every pair must pass active terminology and coverage checks.
Insurer may be one Organization or an array; Claim.insurer selects exactly one.
JSON numeric factors are accepted via Decimal(str(value)); amounts remain exact.
Application rejection reasons (no_coverage_rule, medical_necessity) are encoded
in OperationOutcome.issue.details.coding with the FHIR code business-rule.

The historical pre_validate_200.json example is not guaranteed to pass the new
terminology gate: CPT-only service codes and missing ICD-10-AM catalog entries
are rejected. Use active entries in the supported systems with explicit rules.
