"""Create, evaluate, and retrieve deployment plans.

The planner returns one raw string. This module stores that string unchanged,
validates it, and, when the schema accepts it, runs policy, pricing, and the
canonical hash. It does not approve, reject, render, or re-evaluate a stored plan.
"""

from uuid import UUID

from app.hashing import canonical_hash
from app.models import PlanRecord, PlanStatus
from app.planner import MockPlanner, Planner
from app.policies import evaluate_policies
from app.pricing import estimate_cost
from app.store import InMemoryStore
from app.validation import validate_raw_plan


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
