"""Patient, provider, practitioner and insurance resource subsets."""

from datetime import date
from typing import Literal, Self
from pydantic import Field, field_validator, model_validator
from schemas.fhir_types import (
    CodeableConcept,
    FHIRModel,
    Identifier,
    Period,
    Reference,
    ResourceID,
    Text,
)

NATIONAL_ID_SYSTEM = "http://nphies.sa/identifier/nationalid"
IQAMA_SYSTEM = "http://nphies.sa/identifier/iqama"


class HumanName(FHIRModel):
    use: Literal[
        "usual", "official", "temp", "nickname", "anonymous", "old", "maiden"
    ] = "official"
    family: Text
    given: tuple[Text, ...] = Field(min_length=1, max_length=10)


class Patient(FHIRModel):
    resourceType: Literal["Patient"] = "Patient"
    id: ResourceID
    identifier: tuple[Identifier, ...] = Field(min_length=1, max_length=20)
    name: tuple[HumanName, ...] = Field(min_length=1, max_length=10)
    birthDate: date = Field(
        description="Complete date of birth; future dates rejected."
    )
    gender: Literal["male", "female", "other", "unknown"]

    @field_validator("birthDate", mode="before")
    @classmethod
    def date_only(cls, value: object) -> object:
        if isinstance(value, (int, float, bool)):
            raise ValueError("birthDate must be an ISO date.")
        return value

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.birthDate > date.today():
            raise ValueError("birthDate cannot be in the future.")
        for identifier in self.identifier:
            prefix = {NATIONAL_ID_SYSTEM: "1", IQAMA_SYSTEM: "2"}.get(identifier.system)
            if prefix and (
                len(identifier.value) != 10
                or not identifier.value.isascii()
                or not identifier.value.isdigit()
                or not identifier.value.startswith(prefix)
            ):
                raise ValueError(
                    "National ID/Iqama must contain ten ASCII digits with the correct prefix."
                )
        return self


class Organization(FHIRModel):
    resourceType: Literal["Organization"] = "Organization"
    id: ResourceID
    identifier: tuple[Identifier, ...] = Field(
        min_length=1,
        max_length=20,
        description="Facility or insurer license identifiers with their namespaces.",
    )
    active: bool = Field(default=True, strict=True)
    name: Text
    type: tuple[CodeableConcept, ...] | None = Field(default=None, min_length=1)


class Practitioner(FHIRModel):
    resourceType: Literal["Practitioner"] = "Practitioner"
    id: ResourceID
    identifier: tuple[Identifier, ...] = Field(
        min_length=1,
        max_length=20,
        description="Practitioner registration/license identifiers.",
    )
    name: tuple[HumanName, ...] = Field(min_length=1, max_length=10)


class Coverage(FHIRModel):
    resourceType: Literal["Coverage"] = "Coverage"
    id: ResourceID
    identifier: tuple[Identifier, ...] = Field(min_length=1, max_length=20)
    status: Literal["active", "cancelled", "draft", "entered-in-error"]
    type: CodeableConcept
    subscriberId: Text | None = Field(
        default=None, description="Insurer-issued subscriber identifier."
    )
    beneficiary: Reference = Field(description="Reference to the covered Patient.")
    relationship: CodeableConcept
    payor: tuple[Reference, ...] = Field(
        min_length=1, max_length=1, description="Insurer Organization reference."
    )
    period: Period | None = None
    network: Text | None = None
