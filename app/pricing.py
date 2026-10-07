"""Synthetic monthly cost estimates from the local price catalog.

Prices are synthetic estimates. Every Decimal is built from a catalog string.
A result is either a complete breakdown and exact total, or pricing errors and
no estimate. This module does not change the plan, apply policy, approve, or
render infrastructure.
"""

import json
from decimal import Decimal, DecimalException
from pathlib import Path

from app.models import CostEstimate, CostLineItem, ProposedPlan, Resource, ResourceType

_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "prices.json"
_Catalog = dict[str, dict[str, Decimal]]
_INSTANCE_TYPES = frozenset(
    {item for item in ResourceType if item is not ResourceType.OBJECT_STORAGE}
)


class _CatalogError(Exception):
    """The local price catalog cannot be used."""


def estimate_cost(plan: ProposedPlan, *, catalog_path: Path | None = None) -> CostEstimate:
    """Price every resource, or return errors and no estimate.

    Container and postgres amounts are rate times quantity. Object storage
    amounts are rate times capacity_gb times quantity. Line amounts are not
    rounded. A failed result has pricing errors, no line items, and a zero
    monthly total so a partial sum is not reported.
    """
    path = _CATALOG_PATH if catalog_path is None else catalog_path
    try:
        catalog = _load_catalog(path)
    except _CatalogError as exc:
        return _failed([str(exc)])

    errors: list[str] = []
    line_items: list[CostLineItem] = []
    for index, resource in enumerate(plan.resources):
        line_item, resource_errors = _price_resource(resource, index, catalog)
        errors.extend(resource_errors)
        if line_item is not None:
            line_items.append(line_item)

    if errors or len(line_items) != len(plan.resources):
        return _failed(errors or ["Pricing failed to price every resource."])

    monthly_total = sum((item.amount for item in line_items), Decimal("0"))
    return CostEstimate(
        currency="USD",
        monthly_total=monthly_total,
        line_items=line_items,
        pricing_errors=[],
        succeeded=True,
    )


def _load_catalog(path: Path) -> _Catalog:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _CatalogError(f"Price catalog could not be loaded: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _CatalogError(f"Price catalog could not be loaded: {exc}") from exc
    return _parse_catalog(payload)


def _parse_catalog(payload: object) -> _Catalog:
    if not isinstance(payload, dict):
        raise _CatalogError("Price catalog must be a JSON object.")
    known_types = {item.value for item in ResourceType}
    catalog: _Catalog = {}
    for type_name, rates in payload.items():
        if type_name not in known_types:
            raise _CatalogError(f"Price catalog resource type '{type_name}' is not supported.")
        if not isinstance(rates, dict):
            raise _CatalogError(f"Price catalog entry for '{type_name}' must be a JSON object.")
        parsed: dict[str, Decimal] = {}
        for sku, raw_rate in rates.items():
            if not isinstance(sku, str) or sku.strip() == "":
                raise _CatalogError("Price catalog SKU names must be non-blank strings.")
            parsed[sku] = _parse_rate(sku, raw_rate)
        catalog[type_name] = parsed
    return catalog


def _parse_rate(sku: str, value: object) -> Decimal:
    if not isinstance(value, str):
        raise _CatalogError(f"Catalog rate for SKU '{sku}' must be a string.")
    try:
        rate = Decimal(value)
    except DecimalException as exc:
        raise _CatalogError(f"Catalog rate for SKU '{sku}' must be a finite decimal string.") from exc
    if not rate.is_finite():
        raise _CatalogError(f"Catalog rate for SKU '{sku}' must be finite.")
    if rate < 0:
        raise _CatalogError(f"Catalog rate for SKU '{sku}' must not be negative.")
    return rate


def _price_resource(
    resource: Resource,
    index: int,
    catalog: _Catalog,
) -> tuple[CostLineItem | None, list[str]]:
    errors: list[str] = []
    rate = _lookup_rate(resource, index, catalog, errors)
    capacity_gb = _storage_capacity(resource, index, errors)
    if errors or rate is None:
        return None, errors
    amount = _amount(resource.type, rate, resource.quantity, capacity_gb)
    if amount is None:
        errors.append(f"resources[{index}].type: resource type '{resource.type}' cannot be priced.")
        return None, errors
    return (
        CostLineItem(
            resource_name=resource.name,
            sku=resource.sku,
            quantity=resource.quantity,
            unit_price=rate,
            amount=amount,
        ),
        [],
    )


def _lookup_rate(
    resource: Resource,
    index: int,
    catalog: _Catalog,
    errors: list[str],
) -> Decimal | None:
    type_name = resource.type.value if isinstance(resource.type, ResourceType) else str(resource.type)
    typed_rates = catalog.get(type_name, {})
    if resource.sku in typed_rates:
        return typed_rates[resource.sku]
    field_path = f"resources[{index}].sku"
    if any(resource.sku in rates for rates in catalog.values()):
        errors.append(
            f"{field_path}: SKU '{resource.sku}' does not match resource type "
            f"'{type_name}' on resource '{resource.name}'."
        )
    else:
        errors.append(f"{field_path}: unknown SKU '{resource.sku}' on resource '{resource.name}'.")
    return None


def _storage_capacity(resource: Resource, index: int, errors: list[str]) -> int | None:
    if resource.type is not ResourceType.OBJECT_STORAGE:
        return None
    capacity_gb = resource.capacity_gb
    field_path = f"resources[{index}].capacity_gb"
    if capacity_gb is None:
        errors.append(
            f"{field_path}: object storage '{resource.name}' is missing capacity_gb."
        )
        return None
    if type(capacity_gb) is not int or capacity_gb <= 0:
        errors.append(
            f"{field_path}: object storage '{resource.name}' requires a positive capacity_gb."
        )
        return None
    return capacity_gb


def _amount(
    resource_type: ResourceType,
    rate: Decimal,
    quantity: int,
    capacity_gb: int | None,
) -> Decimal | None:
    units = Decimal(quantity)
    if resource_type is ResourceType.OBJECT_STORAGE:
        if type(capacity_gb) is not int:
            return None
        return rate * Decimal(capacity_gb) * units
    if resource_type in _INSTANCE_TYPES:
        return rate * units
    return None


def _failed(errors: list[str]) -> CostEstimate:
    return CostEstimate(
        currency="USD",
        monthly_total=Decimal("0"),
        line_items=[],
        pricing_errors=list(errors),
        succeeded=False,
    )
