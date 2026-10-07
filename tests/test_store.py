from datetime import datetime, timezone
from time import sleep
from uuid import uuid4

import pytest

from app.models import Environment, PlanRecord, ProposedPlan, Resource, ResourceType
from app.store import DuplicatePlanError, InMemoryStore, PlanNotFoundError


def valid_proposed_plan() -> ProposedPlan:
    return ProposedPlan(
        region="us-east-1",
        environment=Environment.DEV,
        tags={"environment": "dev", "owner": "dev-team", "cost-center": "engineering"},
        resources=[
            Resource(
                type=ResourceType.CONTAINER,
                name="web",
                sku="container-small",
                quantity=2,
            )
        ],
    )


def make_record(**overrides: object) -> PlanRecord:
    data: dict[str, object] = {
        "prompt": "A small PostgreSQL database and two web containers",
        "raw_output": "{}",
        "proposed": valid_proposed_plan(),
    }
    data.update(overrides)
    return PlanRecord.model_validate(data)


def test_create_and_retrieve_record() -> None:
    store = InMemoryStore()
    created = store.create(make_record())
    fetched = store.get(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.prompt == created.prompt
    assert fetched.proposed is not None
    assert fetched.proposed.region == "us-east-1"
    assert fetched.proposed.resources[0].name == "web"


def test_unknown_id_returns_none() -> None:
    store = InMemoryStore()
    assert store.get(uuid4()) is None


def test_duplicate_create_is_rejected() -> None:
    store = InMemoryStore()
    record = make_record()
    store.create(record)
    with pytest.raises(DuplicatePlanError):
        store.create(record)


def test_update_of_missing_record_is_rejected() -> None:
    store = InMemoryStore()
    with pytest.raises(PlanNotFoundError):
        store.update(make_record())


def test_update_preserves_created_at() -> None:
    store = InMemoryStore()
    created = store.create(make_record())
    original_created_at = created.created_at

    updated = store.update(
        created.model_copy(
            update={
                "prompt": "updated prompt",
                "created_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
            }
        )
    )

    assert updated.created_at == original_created_at
    assert store.get(created.id) is not None
    assert store.get(created.id).created_at == original_created_at


def test_update_changes_updated_at() -> None:
    store = InMemoryStore()
    created = store.create(make_record())
    sleep(0.01)
    updated = store.update(created.model_copy(update={"prompt": "changed"}))
    assert updated.updated_at > created.updated_at
    assert updated.prompt == "changed"


def test_mutating_returned_record_does_not_mutate_store() -> None:
    store = InMemoryStore()
    original = make_record()
    created = store.create(original)

    original.prompt = "caller mutated input"
    created.prompt = "mutated returned copy"
    assert created.proposed is not None
    created.proposed.region = "mutated-region"
    created.proposed.resources[0].name = "mutated-name"

    fetched = store.get(created.id)
    assert fetched is not None
    assert fetched.prompt == "A small PostgreSQL database and two web containers"
    assert fetched.proposed is not None
    assert fetched.proposed.region == "us-east-1"
    assert fetched.proposed.resources[0].name == "web"


def test_clear_removes_all_records() -> None:
    store = InMemoryStore()
    first = store.create(make_record())
    second = store.create(make_record())
    store.clear()
    assert store.get(first.id) is None
    assert store.get(second.id) is None
