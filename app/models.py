from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ResourceType(StrEnum):
    CONTAINER = "container"
    POSTGRES = "postgres"
    OBJECT_STORAGE = "object_storage"


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    EVALUATED = "evaluated"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARTIFACT_GENERATED = "artifact_generated"


class CheckStatus(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    ERROR = "error"


def _require_trimmed_length(value: str, *, field_name: str, min_len: int, max_len: int) -> str:
    stripped_len = len(value.strip())
    if stripped_len < min_len or stripped_len > max_len:
        raise ValueError(
            f"{field_name} must contain between {min_len} and {max_len} characters after trimming"
        )
    return value


def _require_nonblank(value: str, *, field_name: str) -> str:
    if value.strip() == "":
        raise ValueError(f"{field_name} must be nonblank")
    return value


def _reject_float(value: object) -> object:
    if isinstance(value, float):
        raise ValueError("Decimal fields must not be converted from float")
    return value


class Resource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ResourceType
    name: str
    sku: str
    quantity: int = Field(strict=True, ge=1, le=100)
    capacity_gb: int | None = Field(default=None, strict=True, gt=0)
    public_access: bool = False

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _require_trimmed_length(value, field_name="name", min_len=1, max_len=64)

    @field_validator("sku")
    @classmethod
    def validate_sku(cls, value: str) -> str:
        return _require_trimmed_length(value, field_name="sku", min_len=1, max_len=64)

    @model_validator(mode="after")
    def validate_capacity_by_type(self) -> "Resource":
        if self.type == ResourceType.OBJECT_STORAGE:
            if self.capacity_gb is None:
                raise ValueError("object_storage requires capacity_gb")
        elif self.capacity_gb is not None:
            raise ValueError(f"{self.type.value} must not provide capacity_gb")
        return self


class ProposedPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region: str
    environment: Environment
    tags: dict[str, str]
    resources: list[Resource] = Field(min_length=1)

    @field_validator("region")
    @classmethod
    def validate_region(cls, value: str) -> str:
        return _require_nonblank(value, field_name="region")

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, tags: dict[str, str]) -> dict[str, str]:
        for key, value in tags.items():
            if key.strip() == "":
                raise ValueError("tag keys must be nonblank")
            if value.strip() == "":
                raise ValueError("tag values must be nonblank")
        return tags


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    field_path: str | None = None


class PolicyCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: str
    status: CheckStatus
    message: str
    resource_name: str | None = None
    field_path: str | None = None


class CostLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_name: str
    sku: str
    quantity: int = Field(strict=True, gt=0)
    unit_price: Decimal = Field(ge=Decimal("0"))
    amount: Decimal = Field(ge=Decimal("0"))

    @field_validator("unit_price", "amount", mode="before")
    @classmethod
    def reject_float_decimals(cls, value: object) -> object:
        return _reject_float(value)


class CostEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str = "USD"
    monthly_total: Decimal
    line_items: list[CostLineItem] = Field(default_factory=list)
    pricing_errors: list[str] = Field(default_factory=list)
    succeeded: bool

    @field_validator("monthly_total", mode="before")
    @classmethod
    def reject_float_total(cls, value: object) -> object:
        return _reject_float(value)

    @model_validator(mode="after")
    def validate_estimate_consistency(self) -> "CostEstimate":
        if self.succeeded:
            if self.pricing_errors:
                raise ValueError("successful cost estimate cannot contain pricing errors")
            line_total = sum((item.amount for item in self.line_items), Decimal("0"))
            if self.monthly_total != line_total:
                raise ValueError("monthly_total must equal the sum of line-item amounts")
        elif not self.pricing_errors:
            raise ValueError("failed cost estimate must contain at least one pricing error")
        return self


class PlanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    prompt: str
    raw_output: str
    status: PlanStatus = PlanStatus.DRAFT
    proposed: ProposedPlan | None = None
    plan_hash: str | None = None
    validation_errors: list[ValidationIssue] = Field(default_factory=list)
    policy_checks: list[PolicyCheck] = Field(default_factory=list)
    cost: CostEstimate | None = None
    artifact: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        return _require_nonblank(value, field_name="prompt")
