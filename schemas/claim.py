"""Claim contracts: exact SAR money and cross-field financial invariants."""

from decimal import Decimal
from typing import Annotated, Literal, Self
import re

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)
from pydantic_core import PydanticCustomError


def parse_money(value: object) -> Decimal:
    # JSON floats may have lost digits before Pydantic sees them. Reject them.
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise PydanticCustomError(
            "money_format",
            "Money must be a decimal string or integer; use '400.00', not a JSON floating-point number.",
        )
    text = str(value)
    if re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,2})?", text) is None:
        raise PydanticCustomError(
            "money_format",
            "Money must be non-negative, finite, and have at most 12 integer digits and two decimal places.",
        )
    return Decimal(text)


Money = Annotated[
    Decimal,
    Field(ge=0, max_digits=14, decimal_places=2, allow_inf_nan=False),
    BeforeValidator(parse_money, json_schema_input_type=str | int),
    PlainSerializer(
        lambda value: format(value, ".2f"), return_type=str, when_used="json"
    ),
]


def halalas(value: Decimal) -> int:
    numerator, denominator = value.as_integer_ratio()
    amount, remainder = divmod(numerator * 100, denominator)
    if remainder:
        raise ValueError("Amount is not an exact number of halalas.")
    return amount


class Issue(BaseModel):
    severity: Literal["error"] = "error"
    code: Literal[
        "business-rule",
        "invalid",
        "required",
        "structure",
        "invariant",
        "login",
        "forbidden",
    ]
    diagnostics: str


class OperationOutcome(BaseModel):
    resourceType: Literal["OperationOutcome"] = "OperationOutcome"
    issue: Annotated[list[Issue], Field(min_length=1)]


class ClaimPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diagnosis_code: str = Field(
        min_length=1, max_length=32, strict=True, examples=["G43"]
    )
    service_code: str = Field(
        min_length=1, max_length=32, strict=True, examples=["70450"]
    )
    insurer_id: Annotated[int, Field(strict=True, gt=0)] | None = Field(
        default=None, examples=[1]
    )
    billed_amount: Money = Field(examples=["500.00"])
    allowed_amount: Money = Field(examples=["400.00"])
    copay: Money = Field(examples=["50.00"])
    net_payable: Money = Field(examples=["350.00"])

    @model_validator(mode="after")
    def verify_finances(self) -> Self:
        if self.billed_amount < self.allowed_amount:
            raise PydanticCustomError(
                "financial_invariant",
                "Billed amount cannot be less than the allowed amount.",
            )
        if self.copay > self.allowed_amount:
            raise PydanticCustomError(
                "financial_invariant", "Copay cannot exceed the allowed amount."
            )
        # Integer subtraction avoids rounding even if Decimal context is changed.
        expected = halalas(self.allowed_amount) - halalas(self.copay)
        if halalas(self.net_payable) != expected:
            raise PydanticCustomError(
                "financial_invariant",
                "Financial math error. net_payable must equal allowed_amount - copay exactly.",
            )
        return self


class ClaimApproval(BaseModel):
    status: Literal["approved"] = "approved"
    message: str
    net_payable: Money
