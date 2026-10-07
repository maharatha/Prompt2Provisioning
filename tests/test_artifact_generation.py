from uuid import UUID, uuid4

import pytest
from jinja2 import TemplateError

from app.artifacts import render_artifact
from app.hashing import canonical_hash
from app.models import (
    CheckStatus,
    PlanRecord,
    PlanStatus,
    PolicyCheck,
    ValidationIssue,
)
from app.services import InvalidPlanOperation, PlanNotFound, PlanService
from app.store import InMemoryStore

EXAMPLE_PROMPT = (
    "A small PostgreSQL database and two web containers for a development team "
    "in US East, optimized for low cost."
)
MEDIUM_DEV_PROMPT = "A medium PostgreSQL database for a development team in US East."


def _service() -> tuple[PlanService, InMemoryStore]:
    store = InMemoryStore()
    return PlanService(store), store


def _created(prompt: str) -> tuple[PlanService, InMemoryStore, PlanRecord]:
    service, store = _service()
    return service, store, service.create_plan(prompt)


def _approved(prompt: str = EXAMPLE_PROMPT) -> tuple[PlanService, InMemoryStore, PlanRecord]:
    service, store, created = _created(prompt)
    assert created.plan_hash is not None
    approved = service.approve_plan(created.id, created.plan_hash)
    return service, store, approved


def _evaluation_snapshot(record: PlanRecord) -> dict[str, object]:
    payload = record.model_dump(mode="json")
    for field in ("status", "artifact", "updated_at"):
        payload.pop(field)
    return payload


def _expect_generation_blocked(service: PlanService, plan_id: UUID, code: str) -> None:
    current = service.get_plan(plan_id)
    assert current is not None
    snapshot = current.model_dump(mode="json")
    with pytest.raises(InvalidPlanOperation) as exc_info:
        service.generate_artifact(plan_id)
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
    assert stored.artifact == current.artifact


def test_approved_plan_generates_retrievable_artifact() -> None:
    service, _store, created = _created(EXAMPLE_PROMPT)
    assert created.status is PlanStatus.EVALUATED
    assert created.plan_hash is not None
    assert created.proposed is not None
    assert created.artifact is None

    approved = service.approve_plan(created.id, created.plan_hash)
    assert approved.status is PlanStatus.APPROVED
    assert approved.artifact is None

    generated = service.generate_artifact(created.id)
    stored = service.get_plan(created.id)

    assert approved.proposed is not None
    assert generated.status is PlanStatus.ARTIFACT_GENERATED
    assert stored is not None
    assert stored.proposed is not None
    assert stored.status is PlanStatus.ARTIFACT_GENERATED
    assert stored.model_dump() == generated.model_dump()
    expected = render_artifact(approved.proposed)
    assert generated.artifact == expected
    assert stored.artifact == expected
    assert stored.artifact == render_artifact(stored.proposed)
    assert _evaluation_snapshot(stored) == _evaluation_snapshot(created)
    assert stored.proposed == created.proposed
    assert stored.plan_hash == created.plan_hash
    assert stored.plan_hash == canonical_hash(stored.proposed)
    assert stored.policy_checks == created.policy_checks
    assert stored.cost == created.cost
    assert stored.validation_errors == created.validation_errors
    assert stored.prompt == created.prompt
    assert stored.raw_output == created.raw_output
    assert stored.created_at == created.created_at

    retrieved = service.get_plan(created.id)
    assert retrieved is not None
    assert retrieved.artifact == stored.artifact
    assert retrieved.status is PlanStatus.ARTIFACT_GENERATED


def test_warning_does_not_block_artifact_generation() -> None:
    service, _store, created = _created(MEDIUM_DEV_PROMPT)
    assert created.plan_hash is not None
    warnings = [check for check in created.policy_checks if check.status is CheckStatus.WARNING]
    assert len(warnings) == 1
    assert warnings[0].policy_id == "dev_medium_cost"
    assert all(check.status is not CheckStatus.ERROR for check in created.policy_checks)

    assert created.proposed is not None
    service.approve_plan(created.id, created.plan_hash)
    generated = service.generate_artifact(created.id)
    stored = service.get_plan(created.id)

    assert generated.status is PlanStatus.ARTIFACT_GENERATED
    assert stored is not None
    assert stored.artifact == render_artifact(created.proposed)
    assert stored.plan_hash == created.plan_hash
    assert stored.policy_checks == created.policy_checks
    assert any(check.status is CheckStatus.WARNING for check in stored.policy_checks)


@pytest.mark.parametrize("kind", ["draft", "evaluated", "rejected", "artifact_generated"])
def test_non_approved_plans_cannot_generate(kind: str) -> None:
    if kind == "draft":
        service, _store, record = _created("SCENARIO:malformed")
        saved_artifact = None
    else:
        service, _store, created = _created(EXAMPLE_PROMPT)
        assert created.plan_hash is not None
        if kind == "evaluated":
            record = created
            saved_artifact = None
        elif kind == "rejected":
            record = service.reject_plan(created.id)
            saved_artifact = None
        else:
            service.approve_plan(created.id, created.plan_hash)
            record = service.generate_artifact(created.id)
            saved_artifact = record.artifact

    assert record.status is not PlanStatus.APPROVED
    _expect_generation_blocked(service, record.id, "status")
    stored = service.get_plan(record.id)
    assert stored is not None
    assert stored.status is record.status
    assert stored.artifact == saved_artifact
    if kind == "artifact_generated":
        assert saved_artifact
        assert stored.artifact == saved_artifact
        retrieved = service.get_plan(record.id)
        assert retrieved is not None
        assert retrieved.artifact == saved_artifact


def test_mutation_after_approval_blocks_generation_and_preserves_hash() -> None:
    service, store, approved = _approved()
    original_hash = approved.plan_hash
    fetched = service.get_plan(approved.id)
    assert fetched is not None
    assert fetched.proposed is not None
    fetched.proposed.region = "us-west-2"
    store.update(fetched)

    _expect_generation_blocked(service, approved.id, "current_hash")
    stored = service.get_plan(approved.id)
    assert stored is not None
    assert stored.status is PlanStatus.APPROVED
    assert stored.artifact is None
    assert stored.plan_hash == original_hash
    assert stored.proposed is not None
    assert stored.proposed.region == "us-west-2"
    assert stored.plan_hash != canonical_hash(stored.proposed)


def test_missing_proposal_blocks_generation() -> None:
    service, store, approved = _approved()
    fetched = service.get_plan(approved.id)
    assert fetched is not None
    fetched.proposed = None
    store.update(fetched)

    _expect_generation_blocked(service, approved.id, "missing_proposal")
    stored = service.get_plan(approved.id)
    assert stored is not None
    assert stored.status is PlanStatus.APPROVED
    assert stored.artifact is None
    assert stored.proposed is None
    assert stored.plan_hash == approved.plan_hash


@pytest.mark.parametrize("stored_hash", [None, ""])
def test_missing_plan_hash_blocks_generation(stored_hash: str | None) -> None:
    service, store, approved = _approved()
    fetched = service.get_plan(approved.id)
    assert fetched is not None
    fetched.plan_hash = stored_hash
    store.update(fetched)

    _expect_generation_blocked(service, approved.id, "missing_plan_hash")
    stored = service.get_plan(approved.id)
    assert stored is not None
    assert stored.status is PlanStatus.APPROVED
    assert stored.artifact is None
    assert stored.plan_hash == stored_hash
    assert stored.proposed == approved.proposed


def test_validation_errors_block_generation() -> None:
    service, store, approved = _approved()
    fetched = service.get_plan(approved.id)
    assert fetched is not None
    fetched.validation_errors.append(
        ValidationIssue(code="injected", message="schema rejected this plan")
    )
    store.update(fetched)

    _expect_generation_blocked(service, approved.id, "validation_errors")
    stored = service.get_plan(approved.id)
    assert stored is not None
    assert stored.status is PlanStatus.APPROVED
    assert stored.artifact is None
    assert stored.validation_errors == fetched.validation_errors


def test_policy_errors_block_generation() -> None:
    service, store, approved = _approved()
    fetched = service.get_plan(approved.id)
    assert fetched is not None
    fetched.policy_checks.append(
        PolicyCheck(
            policy_id="storage_public",
            status=CheckStatus.ERROR,
            message="injected policy error",
        )
    )
    store.update(fetched)

    _expect_generation_blocked(service, approved.id, "policy_error")
    stored = service.get_plan(approved.id)
    assert stored is not None
    assert stored.status is PlanStatus.APPROVED
    assert stored.artifact is None
    assert any(check.status is CheckStatus.ERROR for check in stored.policy_checks)


def test_inconsistent_pricing_blocks_generation() -> None:
    service, store, approved = _approved()
    marked_success = service.get_plan(approved.id)
    assert marked_success is not None
    assert marked_success.cost is not None
    marked_success.cost.pricing_errors.append("injected pricing error")
    store.update(marked_success)
    _expect_generation_blocked(service, approved.id, "pricing")

    failed_service, failed_store, failed = _approved()
    marked_failed = failed_service.get_plan(failed.id)
    assert marked_failed is not None
    assert marked_failed.cost is not None
    marked_failed.cost.succeeded = False
    marked_failed.cost.pricing_errors.clear()
    failed_store.update(marked_failed)
    _expect_generation_blocked(failed_service, failed.id, "pricing")

    missing_service, missing_store, missing_cost = _approved()
    without_cost = missing_service.get_plan(missing_cost.id)
    assert without_cost is not None
    without_cost.cost = None
    missing_store.update(without_cost)
    _expect_generation_blocked(missing_service, missing_cost.id, "pricing")

    total_service, total_store, missing_total = _approved()
    without_total = total_service.get_plan(missing_total.id)
    assert without_total is not None
    assert without_total.cost is not None
    without_total.cost.monthly_total = None
    total_store.update(without_total)
    _expect_generation_blocked(total_service, missing_total.id, "pricing")

    preserved = total_service.get_plan(missing_total.id)
    assert preserved is not None
    assert preserved.status is PlanStatus.APPROVED
    assert preserved.artifact is None
    assert preserved.cost is not None
    assert preserved.cost.monthly_total is None


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("template loader leaked /internal/main.tf.j2"),
        TemplateError("template loader leaked /internal/main.tf.j2"),
    ],
)
def test_renderer_failure_leaves_approved_plan_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    service, _store, approved = _approved()
    current = service.get_plan(approved.id)
    assert current is not None
    snapshot = current.model_dump(mode="json")

    def _fail(_proposed: object) -> str:
        raise failure

    monkeypatch.setattr("app.services.render_artifact", _fail)
    with pytest.raises(InvalidPlanOperation) as exc_info:
        service.generate_artifact(approved.id)

    error = exc_info.value
    assert error.code == "artifact_render"
    assert error.gate == "artifact_render"
    assert error.http_status == 409
    assert "artifact_render" in error.message
    assert "template loader leaked" not in error.message
    assert error.__cause__ is failure
    stored = service.get_plan(approved.id)
    assert stored is not None
    assert stored.model_dump(mode="json") == snapshot
    assert stored.status is PlanStatus.APPROVED
    assert stored.artifact is None
    assert stored.plan_hash == approved.plan_hash
    assert stored.proposed == approved.proposed


def test_missing_plan_is_not_found() -> None:
    service, _store, approved = _approved()
    missing = uuid4()

    with pytest.raises(PlanNotFound) as exc_info:
        service.generate_artifact(missing)

    error = exc_info.value
    assert error.code == "not_found"
    assert error.gate == "not_found"
    assert error.http_status == 404
    assert error.plan_id == missing
    assert "not_found" in error.message
    assert service.get_plan(missing) is None
    preserved = service.get_plan(approved.id)
    assert preserved is not None
    assert preserved.status is PlanStatus.APPROVED
    assert preserved.artifact is None
