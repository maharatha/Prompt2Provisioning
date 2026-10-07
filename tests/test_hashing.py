import hashlib
import json

import pytest

from app.hashing import canonical_hash, canonical_json
from app.models import Environment, ProposedPlan, Resource, ResourceType


def _container(
    name: str = "web",
    sku: str = "container-small",
    quantity: int = 2,
    public_access: bool = False,
) -> Resource:
    return Resource(
        type=ResourceType.CONTAINER,
        name=name,
        sku=sku,
        quantity=quantity,
        public_access=public_access,
    )


def _postgres(
    name: str = "database",
    sku: str = "db-small",
    quantity: int = 1,
) -> Resource:
    return Resource(
        type=ResourceType.POSTGRES,
        name=name,
        sku=sku,
        quantity=quantity,
    )


def _storage(
    name: str = "assets",
    sku: str = "storage-standard",
    quantity: int = 1,
    capacity_gb: int = 100,
    public_access: bool = False,
) -> Resource:
    return Resource(
        type=ResourceType.OBJECT_STORAGE,
        name=name,
        sku=sku,
        quantity=quantity,
        capacity_gb=capacity_gb,
        public_access=public_access,
    )


def _plan(
    *,
    region: str = "us-east-1",
    environment: Environment = Environment.DEV,
    tags: dict[str, str] | None = None,
    resources: list[Resource] | None = None,
) -> ProposedPlan:
    return ProposedPlan(
        region=region,
        environment=environment,
        tags=(
            {
                "environment": "dev",
                "owner": "dev-team",
                "cost-center": "engineering",
            }
            if tags is None
            else tags
        ),
        resources=(
            [_postgres(), _container(), _storage()] if resources is None else resources
        ),
    )


def _documented_canonical_bytes(proposed: ProposedPlan) -> bytes:
    payload = proposed.model_dump(mode="json")
    text = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def test_identical_plans_share_canonical_json_and_hash() -> None:
    first = _plan()
    second = _plan()

    assert canonical_json(first) == canonical_json(second)
    assert canonical_json(first) == canonical_json(first)
    assert canonical_hash(first) == canonical_hash(second)
    assert canonical_hash(first) == canonical_hash(first)


def test_tag_insertion_order_does_not_change_hash() -> None:
    forward = _plan(
        tags={
            "environment": "dev",
            "owner": "dev-team",
            "cost-center": "engineering",
        }
    )
    reverse = _plan(
        tags={
            "cost-center": "engineering",
            "owner": "dev-team",
            "environment": "dev",
        }
    )

    assert list(forward.tags) != list(reverse.tags)
    assert canonical_json(forward) == canonical_json(reverse)
    assert canonical_hash(forward) == canonical_hash(reverse)

    parsed = json.loads(canonical_json(forward))
    assert list(parsed["tags"]) == ["cost-center", "environment", "owner"]
    assert list(forward.tags) == ["environment", "owner", "cost-center"]
    assert list(reverse.tags) == ["cost-center", "owner", "environment"]


def test_resource_list_order_changes_hash() -> None:
    original = _plan()
    reordered = _plan(resources=[_storage(), _container(), _postgres()])

    original_names = [resource.name for resource in original.resources]
    parsed = json.loads(canonical_json(original))
    assert [item["name"] for item in parsed["resources"]] == original_names
    assert canonical_hash(original) != canonical_hash(reordered)


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param(_plan(region="us-west-2"), id="region"),
        pytest.param(_plan(environment=Environment.PROD), id="environment"),
        pytest.param(
            _plan(
                tags={
                    "environment": "dev",
                    "owner": "other-team",
                    "cost-center": "engineering",
                }
            ),
            id="tags",
        ),
        pytest.param(
            _plan(resources=[_postgres(name="primary"), _container(), _storage()]),
            id="resource-name",
        ),
        pytest.param(
            _plan(
                resources=[
                    _postgres(),
                    Resource(
                        type=ResourceType.POSTGRES,
                        name="web",
                        sku="container-small",
                        quantity=2,
                    ),
                    _storage(),
                ]
            ),
            id="resource-type",
        ),
        pytest.param(
            _plan(resources=[_postgres(sku="db-medium"), _container(), _storage()]),
            id="sku",
        ),
        pytest.param(
            _plan(resources=[_postgres(), _container(quantity=3), _storage()]),
            id="quantity",
        ),
        pytest.param(
            _plan(resources=[_postgres(), _container(), _storage(capacity_gb=250)]),
            id="capacity",
        ),
        pytest.param(
            _plan(
                resources=[
                    _postgres(),
                    _container(public_access=True),
                    _storage(),
                ]
            ),
            id="public-access",
        ),
    ],
)
def test_field_change_changes_hash(changed: ProposedPlan) -> None:
    baseline = _plan()

    assert canonical_json(changed) != canonical_json(baseline)
    assert canonical_hash(changed) != canonical_hash(baseline)


def test_unicode_serializes_as_utf8_without_ascii_escapes() -> None:
    plan = _plan(
        region="東京",
        tags={
            "environment": "dev",
            "owner": "café",
            "cost-center": "engineering",
        },
        resources=[_postgres(name="データベース"), _container(), _storage()],
    )

    text = canonical_json(plan)

    assert text == canonical_json(plan)
    assert canonical_hash(plan) == canonical_hash(plan)
    assert "東京" in text
    assert "café" in text
    assert "データベース" in text
    assert "\\u" not in text
    assert canonical_hash(plan) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_digest_matches_sha256_of_documented_canonical_bytes() -> None:
    plan = _plan()
    canonical_bytes = _documented_canonical_bytes(plan)

    assert canonical_json(plan).encode("utf-8") == canonical_bytes
    assert canonical_hash(plan) == hashlib.sha256(canonical_bytes).hexdigest()
    assert canonical_hash(plan) == canonical_hash(plan).lower()
    assert len(canonical_hash(plan)) == 64


def test_canonical_json_includes_every_proposal_field_only() -> None:
    plan = _plan()
    parsed = json.loads(canonical_json(plan))

    assert set(parsed) == {"region", "environment", "tags", "resources"}
    assert parsed["environment"] == "dev"
    postgres, container, storage = parsed["resources"]
    for resource in (postgres, container, storage):
        assert set(resource) == {
            "type",
            "name",
            "sku",
            "quantity",
            "capacity_gb",
            "public_access",
        }
    assert postgres["capacity_gb"] is None
    assert container["public_access"] is False
    assert storage["capacity_gb"] == 100
    assert storage["type"] == "object_storage"


def test_hashing_does_not_mutate_the_plan() -> None:
    plan = _plan()
    before = plan.model_dump(mode="json")
    tag_order = list(plan.tags)
    resource_ids = [id(resource) for resource in plan.resources]
    resource_names = [resource.name for resource in plan.resources]

    canonical_json(plan)
    canonical_hash(plan)

    assert plan.model_dump(mode="json") == before
    assert list(plan.tags) == tag_order
    assert [id(resource) for resource in plan.resources] == resource_ids
    assert [resource.name for resource in plan.resources] == resource_names
