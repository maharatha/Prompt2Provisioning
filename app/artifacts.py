"""Render a validated proposal as a dry-run Terraform-style document.

User-influenced text is quoted and escaped. Resource types and block labels
are chosen here. This module does not check approval, read the store, or
execute Terraform.
"""

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.models import ProposedPlan, Resource, ResourceType

_DEMO_RESOURCE_TYPE = {
    ResourceType.CONTAINER: "demo_container",
    ResourceType.POSTGRES: "demo_postgres",
    ResourceType.MYSQL: "demo_mysql",
    ResourceType.OBJECT_STORAGE: "demo_object_storage",
}
_LABEL_RE = re.compile(r"^(?:container|postgres|mysql|object_storage)_[0-9]+$")


def render_artifact(proposed: ProposedPlan) -> str:
    """Return deterministic fictional HCL for a validated proposal."""
    tags = sorted(proposed.tags.items(), key=lambda item: item[0])
    template_dir = Path(__file__).resolve().parent / "templates"
    environment = Environment(
        loader=FileSystemLoader(template_dir),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=False,
        newline_sequence="\n",
    )
    return environment.get_template("main.tf.j2").render(
        region=_hcl_string(proposed.region),
        environment=_hcl_string(proposed.environment.value),
        tags=[(_hcl_string(key), _hcl_string(value)) for key, value in tags],
        resources=[_resource_view(resource, label) for resource, label in _labeled(proposed)],
    )


def _labeled(proposed: ProposedPlan) -> list[tuple[Resource, str]]:
    ordered = sorted(proposed.resources, key=_resource_sort_key)
    counts: dict[ResourceType, int] = {}
    labeled: list[tuple[Resource, str]] = []
    for resource in ordered:
        index = counts.get(resource.type, 0)
        counts[resource.type] = index + 1
        prefix = _DEMO_RESOURCE_TYPE[resource.type].removeprefix("demo_")
        label = f"{prefix}_{index}"
        if _LABEL_RE.fullmatch(label) is None:
            raise ValueError("block label is not a generated index")
        labeled.append((resource, label))
    return labeled


def _resource_sort_key(resource: Resource) -> tuple[str, str, str, int, bool, int, bool]:
    # None capacity sorts before a stored size. Every resource field is a key.
    capacity = resource.capacity_gb
    return (
        resource.type.value,
        resource.name,
        resource.sku,
        resource.quantity,
        capacity is not None,
        0 if capacity is None else capacity,
        resource.public_access,
    )


def _resource_view(resource: Resource, label: str) -> dict[str, object]:
    demo_type = _DEMO_RESOURCE_TYPE[resource.type]
    include_capacity = resource.type is ResourceType.OBJECT_STORAGE
    view: dict[str, object] = {
        "resource_type": demo_type,
        "label": label,
        "name": _hcl_string(resource.name),
        "sku": _hcl_string(resource.sku),
        "count": resource.quantity,
        "public_access": "true" if resource.public_access else "false",
        "include_capacity": include_capacity,
    }
    if include_capacity:
        view["capacity_gb"] = resource.capacity_gb
    return view


def _hcl_string(value: str) -> str:
    return '"' + _escape_hcl(value) + '"'


def _escape_hcl(value: str) -> str:
    parts: list[str] = []
    for char in value:
        code = ord(char)
        if char == "\\":
            parts.append("\\\\")
        elif char == '"':
            parts.append('\\"')
        elif char == "\n":
            parts.append("\\n")
        elif char == "\r":
            parts.append("\\r")
        elif char == "\t":
            parts.append("\\t")
        elif code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            parts.append(f"\\u{code:04x}")
        else:
            parts.append(char)
    escaped = "".join(parts)
    # Terraform treats ${ and %{ as templates. JSON quoting leaves both intact.
    return escaped.replace("${", "$${").replace("%{", "%%{")
