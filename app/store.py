from datetime import datetime, timezone
from uuid import UUID

from app.models import PlanRecord


class DuplicatePlanError(Exception):
    def __init__(self, plan_id: UUID) -> None:
        self.plan_id = plan_id
        super().__init__(f"plan {plan_id} already exists")


class PlanNotFoundError(Exception):
    def __init__(self, plan_id: UUID) -> None:
        self.plan_id = plan_id
        super().__init__(f"plan {plan_id} was not found")


class InMemoryStore:
    def __init__(self) -> None:
        self._records: dict[UUID, PlanRecord] = {}

    def create(self, record: PlanRecord) -> PlanRecord:
        if record.id in self._records:
            raise DuplicatePlanError(record.id)
        stored = record.model_copy(deep=True)
        self._records[stored.id] = stored
        return stored.model_copy(deep=True)

    def get(self, plan_id: UUID) -> PlanRecord | None:
        stored = self._records.get(plan_id)
        if stored is None:
            return None
        return stored.model_copy(deep=True)

    def update(self, record: PlanRecord) -> PlanRecord:
        existing = self._records.get(record.id)
        if existing is None:
            raise PlanNotFoundError(record.id)
        updated = record.model_copy(deep=True)
        updated.created_at = existing.created_at
        updated.updated_at = datetime.now(timezone.utc)
        self._records[updated.id] = updated
        return updated.model_copy(deep=True)

    def clear(self) -> None:
        self._records.clear()

    def list_records(self) -> list[PlanRecord]:
        return [record.model_copy(deep=True) for record in self._records.values()]
