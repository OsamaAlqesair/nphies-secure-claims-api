"""Shared types for a deliberately bounded FHIR R4 Claim subset.

Decimal strings are accepted at the Python/API boundary. Use to_fhir_json()
for FHIR wire JSON: its decimals are JSON numbers, never binary floats.
"""

from datetime import date, datetime
from decimal import Decimal
import json
import re
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

Text = Annotated[str, Field(strict=True, min_length=1, max_length=256, pattern=r"\S")]
ResourceID = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9\-.]{1,64}$")]
URI = Annotated[
    str,
    Field(strict=True, max_length=2048, pattern=r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$"),
]
Sequence = Annotated[int, Field(strict=True, gt=0, le=2147483647)]


def exact_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(
            "Use Decimal, a decimal string, or an integer; floats are unsafe."
        )
    if not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,6})?", str(value)):
        raise ValueError(
            "Expected a finite nonnegative decimal with at most six decimal places."
        )
    return Decimal(value)


Amount = Annotated[
    Decimal,
    BeforeValidator(exact_decimal, json_schema_input_type=str | int),
    Field(ge=0, max_digits=14, decimal_places=2, allow_inf_nan=False),
]
PositiveDecimal = Annotated[
    Decimal,
    BeforeValidator(exact_decimal, json_schema_input_type=str | int),
    Field(gt=0, max_digits=18, decimal_places=6, allow_inf_nan=False),
]


def parse_factor(value: object) -> Decimal:
    # Convert the decimal spelling, never the binary float expansion.
    return exact_decimal(str(value) if isinstance(value, float) else value)


Factor = Annotated[
    Decimal,
    BeforeValidator(parse_factor, json_schema_input_type=str | int | float),
    Field(ge=0, le=100, max_digits=9, decimal_places=6, allow_inf_nan=False),
]


def _json(value: Any) -> str:
    """Encode only model-dumped values, preserving Decimal numeric tokens."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite FHIR decimal.")
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return json.dumps(value.isoformat())
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(json.dumps(k) + ":" + _json(v) for k, v in value.items())
            + "}"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_json(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class FHIRModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    def to_fhir_json(self) -> str:
        """Use with Response(media_type='application/fhir+json')."""
        return _json(self.model_dump(mode="python", exclude_none=True))

    @classmethod
    def from_fhir_json(cls, payload: str | bytes) -> Self:
        """Decode JSON decimal tokens before a binary-float conversion can occur."""

        def reject_constant(value: str) -> None:
            raise ValueError(f"Invalid JSON number: {value}")

        return cls.model_validate(
            json.loads(payload, parse_float=Decimal, parse_constant=reject_constant)
        )


class Coding(FHIRModel):
    system: URI = Field(description="Canonical coding system URI, not a display label.")
    version: Text | None = Field(
        default=None, description="Terminology release identifier."
    )
    code: Text = Field(description="Code as text; preserve leading zeros.")
    display: Text | None = None


class CodeableConcept(FHIRModel):
    coding: tuple[Coding, ...] = Field(min_length=1, max_length=10)
    text: Text | None = None


class Identifier(FHIRModel):
    system: URI = Field(description="Identifier namespace.")
    value: Text = Field(description="Identifier value; never an integer.")
    type: CodeableConcept | None = None


class Reference(FHIRModel):
    reference: Annotated[
        str, Field(strict=True, min_length=1, max_length=2048, pattern=r"^\S+$")
    ]
    display: Text | None = None


class Money(FHIRModel):
    value: Amount = Field(description="Exact SAR amount; maximum two decimal places.")
    currency: Literal["SAR"] = Field(default="SAR", description="Saudi riyal.")


class Quantity(FHIRModel):
    value: PositiveDecimal = Field(
        description="Positive service quantity, up to six decimals."
    )


class Period(FHIRModel):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError("Period end must not precede start.")
        return self
