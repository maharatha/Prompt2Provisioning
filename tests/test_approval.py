from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.hashing import canonical_hash
from app.models import (
    CheckStatus,
    PlanRecord,
    PlanStatus,
    ValidationIssue,
)
from app.services import InvalidPlanOperation, PlanNotFound, PlanService
from app.store import InMemoryStore

EXAMPLE_PROMPT = (
    "A small PostgreSQL database and two web containers for a development team "
    "in US East, optimized for low cost."
)
MEDIUM_DEV_PROMPT = "A medium PostgreSQL database for a development team in US East."
_WRONG_HASH = "ab" * 32


def _service() -> tuple[PlanService, InMemoryStore]:
    store = InMemoryStore()
    return PlanService(store), store


def _created(prompt: str) -> tuple[PlanService, InMemoryStore, PlanRecord]:
    service, store = _service()
    return service, store, service.create_plan(prompt)


def _expect_approval_blocked(
    service: PlanService,
    plan_id: UUID,
    submitted: object,
    code: str,
) -> None:
    current = service.get_plan(plan_id)
    assert current is not None
    snapshot = current.model_dump(mode="json")
    with pytest.raises(InvalidPlanOperation) as exc_info:
        service.approve_plan(plan_id, submitted)
    error = exc_info.value
    assert error.code == code
    assert error.gate == code
    assert error.http_status == 409
    assert code in error.message
    stored = service.get_plan(plan_id)
    assert stored is not None
    assert stored.model_dump(mode="json") == snapshot
    assert stored.status == current.status
    assert stored.plan_hash == current.plan_hash


def test_valid_plan_can_be_approved() -> None:
    service, _store, created = _created(EXAMPLE_PROMPT)
    assert created.plan_hash is not None
    assert created.proposed is not None

    approved = service.approve_plan(created.id, created.plan_hash)
    stored = service.get_plan(created.id)

    assert approved.status is PlanStatus.APPROVED
    assert stored is not None
    assert stored.status is PlanStatus.APPROVED
    assert approved.plan_hash == created.plan_hash
    assert stored.plan_hash == created.plan_hash
    assert stored.proposed is not None
    assert stored.plan_hash == canonical_hash(stored.proposed)
    assert stored.proposed == created.proposed
    assert stored.policy_checks == created.policy_checks
    assert stored.cost == created.cost
    assert stored.validation_errors == []
    assert stored.prompt == created.prompt
    assert stored.raw_output == created.raw_output
    assert stored.created_at == created.created_at
    assert created.status is PlanStatus.EVALUATED


def test_medium_tier_warning_permits_approval() -> None:
    service, _store, created = _created(MEDIUM_DEV_PROMPT)
    warnings = [check for check in created.policy_checks if check.status is CheckStatus.WARNING]
    errors = [check for check in created.policy_checks if check.status is CheckStatus.ERROR]

    assert created.plan_hash is not None
    assert len(warnings) == 1
    assert warnings[0].policy_id == "dev_medium_cost"
    assert errors == []
    assert created.cost is not None
    assert created.cost.succeeded is True
    assert created.cost.monthly_total == Decimal("95")

    approved = service.approve_plan(created.id, created.plan_hash)
    stored = service.get_plan(created.id)

    assert approved.status is PlanStatus.APPROVED
    assert stored is not None
    assert stored.status is PlanStatus.APPROVED
    assert stored.plan_hash == created.plan_hash
    assert stored.policy_checks == created.policy_checks
    assert any(check.status is CheckStatus.WARNING for check in stored.policy_checks)
    assert all(check.status is not CheckStatus.ERROR for check in stored.policy_checks)


def test_validation_errors_block_approval() -> None:
    service, store, created = _created(EXAMPLE_PROMPT)
    fetched = service.get_plan(created.id)
    assert fetched is not None
    fetched.validation_errors.append(
        ValidationIssue(code="injected", message="schema rejected this plan")
    )
    store.update(fetched)

    _expect_approval_blocked(service, created.id, created.plan_hash, "validation_errors")


def test_policy_errors_block_approval() -> None:
    service, _store, created = _created("SCENARIO:public_storage")

    assert created.status is PlanStatus.EVALUATED
    assert any(check.status is CheckStatus.ERROR for check in created.policy_checks)
    _expect_approval_blocked(service, created.id, created.plan_hash, "policy_error")


def test_failed_pricing_blocks_approval() -> None:
    service, _store, created = _created("SCENARIO:unsupported_sku")

    assert created.cost is not None
    assert created.cost.succeeded is False
    assert created.cost.pricing_errors
    assert all(check.status is not CheckStatus.ERROR for check in created.policy_checks)
    _expect_approval_blocked(service, created.id, created.plan_hash, "pricing")


def test_inconsistent_pricing_blocks_approval() -> None:
    service, store, succeeded = _created(EXAMPLE_PROMPT)
    marked_success = service.get_plan(succeeded.id)
    assert marked_success is not None
    assert marked_success.cost is not None
    marked_success.cost.pricing_errors.append("injected pricing error")
    store.update(marked_success)
    _expect_approval_blocked(service, succeeded.id, succeeded.plan_hash, "pricing")

    _service_failed, store_failed, failed = _created(EXAMPLE_PROMPT)
    marked_failed = _service_failed.get_plan(failed.id)
    assert marked_failed is not None
    assert marked_failed.cost is not None
    marked_failed.cost.succeeded = False
    marked_failed.cost.pricing_errors.clear()
    store_failed.update(marked_failed)
    _expect_approval_blocked(_service_failed, failed.id, failed.plan_hash, "pricing")

    service_missing, store_missing, missing_cost = _created(EXAMPLE_PROMPT)
    without_cost = service_missing.get_plan(missing_cost.id)
    assert without_cost is not None
    without_cost.cost = None
    store_missing.update(without_cost)
    _expect_approval_blocked(
        service_missing,
        missing_cost.id,
        missing_cost.plan_hash,
        "pricing",
    )


@pytest.mark.parametrize(
    "submitted",
    [
        None,
        "",
    ],
)
def test_missing_submitted_hash_blocks_approval(submitted: object) -> None:
    service, _store, created = _created(EXAMPLE_PROMPT)
    _expect_approval_blocked(service, created.id, submitted, "missing_submitted_hash")


@pytest.mark.parametrize(
    "submitted",
    [
        "abc",
        "AB" * 32,
        "g" * 64,
        b"ab" * 32,
        123,
        "ab" * 32 + "\n",
    ],
)
def test_malformed_submitted_hash_blocks_approval(submitted: object) -> None:
    service, _store, created = _created(EXAMPLE_PROMPT)
    _expect_approval_blocked(service, created.id, submitted, "malformed_submitted_hash")


def test_wrong_submitted_hash_blocks_approval() -> None:
    service, _store, created = _created(EXAMPLE_PROMPT)
    assert created.plan_hash != _WRONG_HASH
    _expect_approval_blocked(service, created.id, _WRONG_HASH, "submitted_hash")


def test_non_ascii_submitted_hash_blocks_approval() -> None:
    service, _store, created = _created(EXAMPLE_PROMPT)
    _expect_approval_blocked(
        service,
        created.id,
        "é" + ("a" * 63),
        "non_ascii_submitted_hash",
    )


def test_missing_stored_hash_blocks_approval() -> None:
    service, store, created = _created(EXAMPLE_PROMPT)
    fetched = service.get_plan(created.id)
    assert fetched is not None
    fetched.plan_hash = None
    store.update(fetched)

    _expect_approval_blocked(service, created.id, created.plan_hash, "missing_plan_hash")
    stored = service.get_plan(created.id)
    assert stored is not None
    assert stored.plan_hash is None
    assert stored.status is PlanStatus.EVALUATED


def test_missing_proposal_blocks_approval() -> None:
    service, store, created = _created(EXAMPLE_PROMPT)
    fetched = service.get_plan(created.id)
    assert fetched is not None
    fetched.proposed = None
    store.update(fetched)

    _expect_approval_blocked(service, created.id, created.plan_hash, "missing_proposal")
    stored = service.get_plan(created.id)
    assert stored is not None
    assert stored.proposed is None
    assert stored.plan_hash == created.plan_hash
    assert stored.status is PlanStatus.EVALUATED


def test_server_side_mutation_blocks_approval_with_original_hash() -> None:
    service, store, created = _created(EXAMPLE_PROMPT)
    original_hash = created.plan_hash
    fetched = service.get_plan(created.id)
    assert fetched is not None
    assert fetched.proposed is not None
    fetched.proposed.region = "us-west-2"
    store.update(fetched)

    _expect_approval_blocked(service, created.id, original_hash, "current_hash")
    stored = service.get_plan(created.id)
    assert stored is not None
    assert stored.status is PlanStatus.EVALUATED
    assert stored.plan_hash == original_hash
    assert stored.proposed is not None
    assert stored.proposed.region == "us-west-2"
    assert stored.plan_hash != canonical_hash(stored.proposed)


def test_schema_invalid_mutation_fails_cleanly() -> None:
    service, store, created = _created(EXAMPLE_PROMPT)
    original_hash = created.plan_hash
    fetched = service.get_plan(created.id)
    assert fetched is not None
    assert fetched.proposed is not None
    fetched.proposed.resources[0].quantity = 0
    store.update(fetched)

    _expect_approval_blocked(service, created.id, original_hash, "current_hash")
    stored = service.get_plan(created.id)
    assert stored is not None
    assert stored.status is PlanStatus.EVALUATED
    assert stored.plan_hash == original_hash
    assert stored.proposed is not None
    assert stored.proposed.resources[0].quantity == 0

    service_bad, store_bad, unhashable = _created(EXAMPLE_PROMPT)
    bad = service_bad.get_plan(unhashable.id)
    assert bad is not None
    assert bad.proposed is not None
    bad.proposed.resources[0].quantity = object()  # type: ignore[assignment]
    store_bad.update(bad)

    with pytest.raises(InvalidPlanOperation) as exc_info:
        service_bad.approve_plan(unhashable.id, unhashable.plan_hash)
    assert exc_info.value.code == "current_hash"
    assert exc_info.value.http_status == 409
    preserved = service_bad.get_plan(unhashable.id)
    assert preserved is not None
    assert preserved.proposed is not None
    assert type(preserved.proposed.resources[0].quantity) is object
    assert preserved.status is PlanStatus.EVALUATED
    assert preserved.plan_hash == unhashable.plan_hash


def test_failed_approval_preserves_status_and_stored_hash() -> None:
    service, _store, created = _created(EXAMPLE_PROMPT)
    _expect_approval_blocked(service, created.id, _WRONG_HASH, "submitted_hash")
    stored = service.get_plan(created.id)
    assert stored is not None
    assert stored.status is PlanStatus.EVALUATED
    assert stored.plan_hash == created.plan_hash


def test_draft_and_evaluated_plans_can_be_rejected() -> None:
    draft_service, _draft_store, draft = _created("SCENARIO:malformed")
    rejected_draft = draft_service.reject_plan(draft.id)
    stored_draft = draft_service.get_plan(draft.id)

    assert draft.status is PlanStatus.DRAFT
    assert rejected_draft.status is PlanStatus.REJECTED
    assert stored_draft is not None
    assert stored_draft.status is PlanStatus.REJECTED
    assert stored_draft.proposed is None
    assert stored_draft.plan_hash is None
    assert stored_draft.validation_errors == draft.validation_errors
    assert stored_draft.policy_checks == []
    assert stored_draft.cost is None
    assert stored_draft.prompt == draft.prompt
    assert stored_draft.raw_output == draft.raw_output
    assert stored_draft.created_at == draft.created_at

    service, _store, created = _created(EXAMPLE_PROMPT)
    rejected = service.reject_plan(created.id)
    stored = service.get_plan(created.id)

    assert rejected.status is PlanStatus.REJECTED
    assert stored is not None
    assert stored.status is PlanStatus.REJECTED
    assert stored.proposed == created.proposed
    assert stored.plan_hash == created.plan_hash
    assert stored.policy_checks == created.policy_checks
    assert stored.cost == created.cost
    assert stored.validation_errors == created.validation_errors
    assert stored.prompt == created.prompt
    assert stored.raw_output == created.raw_output
    assert stored.created_at == created.created_at


def test_rejected_plan_cannot_be_approved() -> None:
    service, _store, created = _created(EXAMPLE_PROMPT)
    rejected = service.reject_plan(created.id)
    assert rejected.status is PlanStatus.REJECTED

    _expect_approval_blocked(service, created.id, created.plan_hash, "status")
    stored = service.get_plan(created.id)
    assert stored is not None
    assert stored.status is PlanStatus.REJECTED
    assert stored.plan_hash == created.plan_hash
    assert stored.proposed == created.proposed

    snapshot = stored.model_dump(mode="json")
    with pytest.raises(InvalidPlanOperation) as exc_info:
        service.reject_plan(created.id)
    assert exc_info.value.code == "status"
    assert exc_info.value.http_status == 409
    again = service.get_plan(created.id)
    assert again is not None
    assert again.model_dump(mode="json") == snapshot


def test_approved_plan_cannot_be_approved_or_rejected_again() -> None:
    service, _store, created = _created(EXAMPLE_PROMPT)
    approved = service.approve_plan(created.id, created.plan_hash)
    assert approved.status is PlanStatus.APPROVED

    _expect_approval_blocked(service, created.id, created.plan_hash, "status")
    snapshot = service.get_plan(created.id)
    assert snapshot is not None
    before = snapshot.model_dump(mode="json")
    with pytest.raises(InvalidPlanOperation) as exc_info:
        service.reject_plan(created.id)
    assert exc_info.value.code == "status"
    stored = service.get_plan(created.id)
    assert stored is not None
    assert stored.model_dump(mode="json") == before
    assert stored.status is PlanStatus.APPROVED
    assert stored.plan_hash == created.plan_hash


def test_artifact_generated_plan_cannot_be_approved_or_rejected() -> None:
    service, store, created = _created(EXAMPLE_PROMPT)
    fetched = service.get_plan(created.id)
    assert fetched is not None
    fetched.status = PlanStatus.ARTIFACT_GENERATED
    store.update(fetched)

    _expect_approval_blocked(service, created.id, created.plan_hash, "status")
    current = service.get_plan(created.id)
    assert current is not None
    before = current.model_dump(mode="json")
    with pytest.raises(InvalidPlanOperation) as exc_info:
        service.reject_plan(created.id)
    assert exc_info.value.code == "status"
    stored = service.get_plan(created.id)
    assert stored is not None
    assert stored.model_dump(mode="json") == before
    assert stored.status is PlanStatus.ARTIFACT_GENERATED
    assert stored.plan_hash == created.plan_hash


def test_draft_cannot_be_approved() -> None:
    service, _store, draft = _created("SCENARIO:malformed")
    assert draft.status is PlanStatus.DRAFT
    _expect_approval_blocked(service, draft.id, _WRONG_HASH, "status")


def test_missing_ids_are_not_found() -> None:
    service, _store, created = _created(EXAMPLE_PROMPT)
    missing = uuid4()

    with pytest.raises(PlanNotFound) as approve_error:
        service.approve_plan(missing, created.plan_hash)
    assert approve_error.value.code == "not_found"
    assert approve_error.value.gate == "not_found"
    assert approve_error.value.http_status == 404
    assert approve_error.value.plan_id == missing
    assert "not_found" in approve_error.value.message

    with pytest.raises(PlanNotFound) as reject_error:
        service.reject_plan(missing)
    assert reject_error.value.code == "not_found"
    assert reject_error.value.http_status == 404
    assert reject_error.value.plan_id == missing

    assert service.get_plan(missing) is None
    assert service.get_plan(created.id) is not None
