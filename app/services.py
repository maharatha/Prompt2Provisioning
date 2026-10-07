"""Create, evaluate, retrieve, approve, reject, and render deployment plans.

The planner returns one raw string. This module stores that string unchanged,
validates it, and, when the schema accepts it, runs policy, pricing, and the
canonical hash. Approval, rejection, and artifact generation change status
only. They do not re-evaluate a stored plan, repair it, or replace
``plan_hash``. Rendering runs only for an approved plan, and the artifact is
stored only after rendering succeeds.
"""

import hmac
from uuid import UUID

from app.artifacts import render_artifact
from app.hashing import canonical_hash
from app.models import CheckStatus, PlanRecord, PlanStatus, ProposedPlan
from app.planner import MockPlanner, Planner
from app.policies import evaluate_policies
from app.pricing import estimate_cost
from app.store import InMemoryStore
from app.validation import validate_raw_plan

_HASH_LENGTH = 64
_HASH_ALPHABET = frozenset("0123456789abcdef")


class ServiceError(Exception):
    """A failed plan operation, with a code the API can map to HTTP.

    ``not_found`` maps to 404. Every other code names the gate that failed and
    maps to 409. ``message`` includes that same code.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.gate = code
        self.message = message
        self.http_status = 404 if code == "not_found" else 409
        super().__init__(message)


class PlanNotFound(ServiceError):
    """The requested plan id is not in the store."""

    def __init__(self, plan_id: UUID) -> None:
        self.plan_id = plan_id
        super().__init__("not_found", f"not_found: Plan {plan_id} was not found.")


class InvalidPlanOperation(ServiceError):
    """The plan exists, and the requested transition is not allowed."""

    def __init__(self, gate: str, detail: str) -> None:
        super().__init__(gate, f"{gate}: {detail}")


class PlanService:
    def __init__(self, store: InMemoryStore, planner: Planner | None = None) -> None:
        self._store = store
        self._planner = MockPlanner() if planner is None else planner

    def create_plan(self, prompt: str) -> PlanRecord:
        raw_output = self._planner.generate(prompt)
        validation = validate_raw_plan(raw_output)
        proposed = validation.proposed
        if proposed is None:
            return self._store.create(
                PlanRecord(
                    prompt=prompt,
                    raw_output=raw_output,
                    status=PlanStatus.DRAFT,
                    validation_errors=list(validation.errors),
                )
            )

        return self._store.create(
            PlanRecord(
                prompt=prompt,
                raw_output=raw_output,
                status=PlanStatus.EVALUATED,
                proposed=proposed,
                plan_hash=canonical_hash(proposed),
                policy_checks=evaluate_policies(proposed),
                cost=estimate_cost(proposed),
            )
        )

    def get_plan(self, plan_id: UUID) -> PlanRecord | None:
        return self._store.get(plan_id)

    def approve_plan(self, plan_id: UUID, plan_hash: object) -> PlanRecord:
        """Approve an evaluated plan when every approval gate passes.

        The stored hash is compared and then recomputed. Neither value is
        written back. A failing gate leaves the stored record unchanged.
        """
        record = self._require_record(plan_id)
        self._require_evaluated(record)
        stored_hash = self._require_proposal_and_hash(record)
        self._require_submitted_hash(plan_hash, stored_hash)
        self._require_current_hash(record, stored_hash)
        self._require_no_validation_errors(record)
        self._require_no_policy_errors(record)
        self._require_successful_pricing(record)
        approved = record.model_copy(update={"status": PlanStatus.APPROVED})
        return self._store.update(approved)

    def reject_plan(self, plan_id: UUID) -> PlanRecord:
        """Reject a draft or evaluated plan without changing its evaluation.

        Approved, rejected, and artifact-generated plans are refused. The
        proposal, hash, validation errors, policy checks, and cost stay as
        stored.
        """
        record = self._require_record(plan_id)
        if record.status not in (PlanStatus.DRAFT, PlanStatus.EVALUATED):
            raise InvalidPlanOperation(
                "status",
                (
                    "Only a draft or evaluated plan can be rejected; "
                    f"the plan is '{record.status.value}'."
                ),
            )
        rejected = record.model_copy(update={"status": PlanStatus.REJECTED})
        return self._store.update(rejected)

    def generate_artifact(self, plan_id: UUID) -> PlanRecord:
        """Render and store HCL for an approved plan.

        The evaluated hash is recomputed and compared, then left unchanged.
        The proposal is not modified. A failing gate or a renderer error
        leaves the stored record unchanged. A plan that already has an
        artifact is refused; retrieve it with ``get_plan``.
        """
        record = self._require_record(plan_id)
        self._require_approved(record)
        stored_hash = self._require_proposal_and_hash(record)
        self._require_current_hash(record, stored_hash)
        self._require_no_validation_errors(record)
        self._require_no_policy_errors(record)
        self._require_successful_pricing(record)
        proposed = record.proposed
        if not isinstance(proposed, ProposedPlan):
            raise InvalidPlanOperation(
                "missing_proposal",
                "The stored plan has no proposal.",
            )
        rendered = _render_artifact(proposed)
        generated = record.model_copy(
            update={
                "status": PlanStatus.ARTIFACT_GENERATED,
                "artifact": rendered,
            }
        )
        return self._store.update(generated)

    def _require_record(self, plan_id: UUID) -> PlanRecord:
        record = self._store.get(plan_id)
        if record is None:
            raise PlanNotFound(plan_id)
        return record

    def _require_evaluated(self, record: PlanRecord) -> None:
        if record.status is not PlanStatus.EVALUATED:
            raise InvalidPlanOperation(
                "status",
                (
                    "Approval requires status 'evaluated'; "
                    f"the plan is '{record.status.value}'."
                ),
            )

    def _require_approved(self, record: PlanRecord) -> None:
        if record.status is not PlanStatus.APPROVED:
            raise InvalidPlanOperation(
                "status",
                (
                    "Artifact generation requires status 'approved'; "
                    f"the plan is '{record.status.value}'."
                ),
            )

    def _require_proposal_and_hash(self, record: PlanRecord) -> str:
        if record.proposed is None:
            raise InvalidPlanOperation(
                "missing_proposal",
                "The stored plan has no proposal.",
            )
        stored_hash = record.plan_hash
        if not isinstance(stored_hash, str) or stored_hash == "":
            raise InvalidPlanOperation(
                "missing_plan_hash",
                "The stored plan has no plan hash.",
            )
        return stored_hash

    def _require_submitted_hash(self, submitted: object, stored_hash: str) -> None:
        problem = _submitted_hash_problem(submitted)
        if problem == "missing_submitted_hash":
            raise InvalidPlanOperation(
                problem,
                "A plan hash is required to approve a plan.",
            )
        if problem == "non_ascii_submitted_hash":
            raise InvalidPlanOperation(
                problem,
                "The submitted plan hash must contain only ASCII characters.",
            )
        if problem == "malformed_submitted_hash":
            raise InvalidPlanOperation(
                problem,
                "The submitted plan hash must be 64 lowercase hexadecimal characters.",
            )
        if not isinstance(submitted, str) or not _hashes_equal(submitted, stored_hash):
            raise InvalidPlanOperation(
                "submitted_hash",
                "The submitted plan hash does not match the stored plan hash.",
            )

    def _require_current_hash(self, record: PlanRecord, stored_hash: str) -> None:
        proposed = record.proposed
        if not isinstance(proposed, ProposedPlan):
            raise InvalidPlanOperation(
                "current_hash",
                "The stored proposal cannot be hashed.",
            )
        try:
            current_hash = canonical_hash(proposed)
        except (AttributeError, TypeError, ValueError) as exc:
            raise InvalidPlanOperation(
                "current_hash",
                "The stored proposal cannot be hashed.",
            ) from exc
        if not _hashes_equal(current_hash, stored_hash):
            raise InvalidPlanOperation(
                "current_hash",
                "The stored proposal no longer matches the evaluated plan hash.",
            )

    def _require_no_validation_errors(self, record: PlanRecord) -> None:
        if record.validation_errors:
            raise InvalidPlanOperation(
                "validation_errors",
                "The plan has validation errors.",
            )

    def _require_no_policy_errors(self, record: PlanRecord) -> None:
        if any(check.status is CheckStatus.ERROR for check in record.policy_checks):
            raise InvalidPlanOperation(
                "policy_error",
                "A policy result has status error.",
            )

    def _require_successful_pricing(self, record: PlanRecord) -> None:
        cost = record.cost
        if cost is None or cost.succeeded is not True or bool(cost.pricing_errors):
            raise InvalidPlanOperation(
                "pricing",
                "Pricing did not succeed.",
            )
        if cost.monthly_total is None:
            raise InvalidPlanOperation(
                "pricing",
                "Pricing did not succeed.",
            )


def _submitted_hash_problem(submitted: object) -> str | None:
    if submitted is None or submitted == "":
        return "missing_submitted_hash"
    if not isinstance(submitted, str):
        return "malformed_submitted_hash"
    if not submitted.isascii():
        return "non_ascii_submitted_hash"
    if len(submitted) != _HASH_LENGTH or any(char not in _HASH_ALPHABET for char in submitted):
        return "malformed_submitted_hash"
    return None


def _render_artifact(proposed: ProposedPlan) -> str:
    """Render one proposal, mapping a renderer failure to a service error.

    The ``except`` covers only ``render_artifact``. Gate failures in
    ``generate_artifact`` are raised before this call and are not caught here.
    """
    try:
        return render_artifact(proposed)
    except Exception as exc:
        raise InvalidPlanOperation(
            "artifact_render",
            "The approved plan could not be rendered.",
        ) from exc


def _hashes_equal(left: str, right: str) -> bool:
    """Compare two hashes with hmac.compare_digest.

    compare_digest raises TypeError when a str is not ASCII. Callers reject
    those inputs first. This guard is a second check so a bad stored hash
    cannot escape as that TypeError.
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if not left.isascii() or not right.isascii():
        return False
    return hmac.compare_digest(left, right)
