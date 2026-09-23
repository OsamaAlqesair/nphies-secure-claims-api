"""FHIR R4 Claim subset for pre-validation, not a complete NPHIES message.

No meta.profile conformance is asserted. Production submission additionally
requires the selected NPHIES profile, terminology validation, Encounter,
MessageHeader, and a message Bundle. CPT acceptance here is not NPHIES approval.
Sources:
https://portal.nphies.sa/ig/usecases.html
https://portal.nphies.sa/ig/StructureDefinition-claim-base.html
"""

from datetime import date
from decimal import Decimal, localcontext
from typing import Annotated, Literal, Self
from pydantic import AwareDatetime, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError
from schemas.fhir_types import (
    Amount,
    CodeableConcept,
    Coding,
    Factor,
    FHIRModel,
    Identifier,
    Money,
    Quantity,
    Reference,
    ResourceID,
    Sequence,
    Text,
    URI,
)
from schemas.fhir_resources import Coverage, Organization, Patient, Practitioner

TAX_URL = "http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition/extension-tax"


class DiagnosisCoding(Coding):
    system: Literal["http://hl7.org/fhir/sid/icd-10-am"] = (
        "http://hl7.org/fhir/sid/icd-10-am"
    )
    code: Annotated[
        str, Field(strict=True, pattern=r"^[A-Z][0-9]{2}(?:\.[0-9]{1,2})?$")
    ] = Field(
        description="ICD-10-AM lexical shape only; validate membership against a licensed release.",
        examples=["E11.9", "K35.80"],
    )


class DiagnosisConcept(CodeableConcept):
    coding: tuple[DiagnosisCoding, ...] = Field(min_length=1, max_length=1)


class ClaimDiagnosis(FHIRModel):
    sequence: Sequence
    diagnosisCodeableConcept: DiagnosisConcept
    type: tuple[CodeableConcept, ...] = Field(min_length=1, max_length=5)


class ServiceCoding(Coding):
    system: Literal[
        "http://nphies.sa/terminology/CodeSystem/services",
        "http://nphies.sa/terminology/CodeSystem/procedures",
        "http://nphies.sa/terminology/CodeSystem/laboratory",
        "http://www.ama-assn.org/go/cpt",
    ] = Field(
        description="Supported subset of SBS systems, or CPT for internal intake."
    )
    code: Annotated[
        str, Field(strict=True, min_length=1, max_length=32, pattern=r"^[A-Za-z0-9-]+$")
    ]


class ServiceConcept(CodeableConcept):
    coding: tuple[ServiceCoding, ...] = Field(min_length=1, max_length=10)


class ClaimItemExtension(FHIRModel):
    """Supported flat item extension value choices; clinical rules remain separate."""

    url: URI
    valueMoney: Money | None = None
    valueBoolean: Annotated[bool, Field(strict=True)] | None = None
    valueString: Text | None = None
    valueIdentifier: Identifier | None = None
    valueCodeableConcept: CodeableConcept | None = None
    valueReference: Reference | None = None

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        values = (
            self.valueMoney,
            self.valueBoolean,
            self.valueString,
            self.valueIdentifier,
            self.valueCodeableConcept,
            self.valueReference,
        )
        if sum(value is not None for value in values) != 1:
            raise ValueError("Extension requires exactly one supported value[x].")
        if self.url.endswith("extension-tax") and self.valueMoney is None:
            raise ValueError("Tax extension requires valueMoney.")
        return self


class TaxExtension(ClaimItemExtension):
    url: URI = TAX_URL
    valueMoney: Money


class LineFinancials(FHIRModel):
    """Internal computed breakdown; not an extra FHIR Claim.item property."""

    gross: Amount = Field(
        description="Quantity times unit price, before factor and tax."
    )
    net: Amount = Field(description="Gross times factor, before tax.")
    tax: Amount = Field(description="Explicit tax amount; no hardcoded VAT rate.")
    total: Amount = Field(description="Net plus tax; corresponds to NPHIES item.net.")


class ClaimItem(FHIRModel):
    sequence: Sequence
    diagnosisSequence: tuple[Sequence, ...] = Field(min_length=1, max_length=100)
    careTeamSequence: tuple[Sequence, ...] = Field(min_length=1, max_length=100)
    productOrService: ServiceConcept
    servicedDate: date = Field(description="Date the service was provided.")
    quantity: Quantity
    unitPrice: Money
    factor: Factor = Field(
        default=Decimal("1"), description="Explicit price multiplier."
    )
    extension: tuple[ClaimItemExtension, ...] = Field(
        default=(),
        max_length=20,
        description="Optional extensions; omitted tax is zero. Duplicate tax is rejected.",
    )
    net: Money = Field(description="NPHIES tax-inclusive line total, not pre-tax net.")

    def financials(self) -> LineFinancials:
        # Bounded operands fit exactly within 50 digits regardless of ambient context.
        # No implicit rounding policy: fractional halalas are rejected.
        with localcontext() as context:
            context.prec = 50
            gross = self.quantity.value * self.unitPrice.value
            net = gross * self.factor
            tax = next(
                (
                    entry.valueMoney.value
                    for entry in self.extension
                    if entry.url.endswith("extension-tax")
                ),
                Decimal("0"),
            )
            return LineFinancials(gross=gross, net=net, tax=tax, total=net + tax)

    @model_validator(mode="after")
    def validate_finances(self) -> Self:
        for name in ("diagnosisSequence", "careTeamSequence"):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"{name} contains duplicates.")
        if sum(entry.url.endswith("extension-tax") for entry in self.extension) > 1:
            raise PydanticCustomError(
                "financial_invariant", "Only one tax extension is allowed."
            )
        try:
            expected_total = self.financials().total
        except ValidationError:
            raise PydanticCustomError(
                "financial_invariant",
                "Calculated amounts must be exact SAR amounts with at most two decimal places.",
            ) from None
        if self.net.value != expected_total:
            raise PydanticCustomError(
                "financial_invariant",
                "item.net must equal quantity * unitPrice * factor + tax exactly.",
            )
        return self


class ClaimCareTeam(FHIRModel):
    sequence: Sequence
    provider: Reference = Field(
        description="Reference to the responsible Practitioner."
    )
    responsible: bool = Field(default=True, strict=True)
    role: CodeableConcept | None = None


class ClaimInsurance(FHIRModel):
    sequence: Sequence
    focal: bool = Field(strict=True)
    coverage: Reference
    preAuthRef: tuple[Text, ...] | None = Field(
        default=None, min_length=1, max_length=20
    )


class Claim(FHIRModel):
    resourceType: Literal["Claim"] = "Claim"
    id: ResourceID
    identifier: tuple[Identifier, ...] = Field(min_length=1, max_length=20)
    status: Literal["active", "cancelled", "draft", "entered-in-error"]
    type: CodeableConcept
    subType: CodeableConcept
    use: Literal["claim"] = "claim"
    patient: Reference
    created: AwareDatetime = Field(description="Creation timestamp including timezone.")
    provider: Reference = Field(description="Facility Organization reference.")
    insurer: Reference = Field(description="Insurer Organization reference.")
    priority: CodeableConcept
    careTeam: tuple[ClaimCareTeam, ...] = Field(min_length=1, max_length=100)
    insurance: tuple[ClaimInsurance, ...] = Field(min_length=1, max_length=20)
    diagnosis: tuple[ClaimDiagnosis, ...] = Field(min_length=1, max_length=100)
    item: tuple[ClaimItem, ...] = Field(min_length=1, max_length=1000)
    total: Money

    @model_validator(mode="after")
    def consistency(self) -> Self:
        for name in ("careTeam", "insurance", "diagnosis", "item"):
            sequences = [entry.sequence for entry in getattr(self, name)]
            if len(sequences) != len(set(sequences)):
                raise ValueError(f"{name} sequence values must be unique.")
        if sum(entry.focal for entry in self.insurance) != 1:
            raise ValueError("Exactly one insurance entry must be focal.")
        diagnoses = {entry.sequence for entry in self.diagnosis}
        practitioners = {entry.sequence for entry in self.careTeam}
        for item in self.item:
            if not set(item.diagnosisSequence) <= diagnoses:
                raise ValueError("Item references an unknown diagnosis sequence.")
            if not set(item.careTeamSequence) <= practitioners:
                raise ValueError("Item references an unknown care team sequence.")
        with localcontext() as context:
            context.prec = 50
            if self.total.value != sum(
                (item.net.value for item in self.item), Decimal(0)
            ):
                raise PydanticCustomError(
                    "financial_invariant",
                    "Claim.total must equal the sum of item.net exactly.",
                )
        return self


class ClaimSubmission(FHIRModel):
    """Internal intake envelope, NOT a FHIR Bundle or an on-wire Claim resource.

    Resources are separate and linked by relative ResourceType/id references.
    Convert to the applicable NPHIES message Bundle in a separate adapter.
    """

    claim: Claim
    patient: Patient
    provider: Organization
    insurer: Organization
    practitioners: tuple[Practitioner, ...] = Field(min_length=1, max_length=100)
    coverages: tuple[Coverage, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="before")
    @classmethod
    def resolve_insurer_array(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get("insurer"), list):
            return value
        resources = value["insurer"]
        if not 1 <= len(resources) <= 20:
            raise ValueError("Expected between one and twenty insurer resources.")
        insurers = [Organization.model_validate(resource) for resource in resources]
        if len({insurer.id for insurer in insurers}) != len(insurers):
            raise ValueError("Duplicate insurer resource identity.")
        claim = value.get("claim")
        reference = claim.get("insurer", {}) if isinstance(claim, dict) else {}
        reference = reference.get("reference") if isinstance(reference, dict) else None
        matches = [
            insurer for insurer in insurers if f"Organization/{insurer.id}" == reference
        ]
        if len(matches) != 1:
            raise ValueError(
                "Claim insurer reference must resolve to exactly one supplied insurer."
            )
        return {**value, "insurer": matches[0]}

    @model_validator(mode="after")
    def references(self) -> Self:
        resources = (
            self.patient,
            self.provider,
            self.insurer,
            *self.practitioners,
            *self.coverages,
        )
        keys = [f"{resource.resourceType}/{resource.id}" for resource in resources]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate resource identity in submission.")
        expected = (
            (self.claim.patient, f"Patient/{self.patient.id}"),
            (self.claim.provider, f"Organization/{self.provider.id}"),
            (self.claim.insurer, f"Organization/{self.insurer.id}"),
        )
        if any(reference.reference != key for reference, key in expected):
            raise ValueError(
                "Claim patient/provider/insurer reference does not match supplied resource."
            )
        practitioner_refs = {f"Practitioner/{entry.id}" for entry in self.practitioners}
        coverage_refs = {f"Coverage/{entry.id}" for entry in self.coverages}
        if any(
            entry.provider.reference not in practitioner_refs
            for entry in self.claim.careTeam
        ):
            raise ValueError("Unresolved practitioner reference.")
        if any(
            entry.coverage.reference not in coverage_refs
            for entry in self.claim.insurance
        ):
            raise ValueError("Unresolved coverage reference.")
        for coverage in self.coverages:
            if coverage.beneficiary.reference != self.claim.patient.reference:
                raise ValueError("Coverage beneficiary must match Claim.patient.")
        focal = next(entry for entry in self.claim.insurance if entry.focal)
        focal_coverage = next(
            entry
            for entry in self.coverages
            if f"Coverage/{entry.id}" == focal.coverage.reference
        )
        if focal_coverage.payor[0].reference != self.claim.insurer.reference:
            raise ValueError("Focal coverage payor must match Claim.insurer.")
        return self
