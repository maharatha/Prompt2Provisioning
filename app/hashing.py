"""Canonical JSON and SHA-256 for a validated proposed plan.

The digest covers the proposal only. Record identity, status, timestamps,
planner text, policies, and cost are not part of the bytes. These functions
do not mutate the plan, read the store, or refresh a stored hash.
"""

import hashlib
import json

from app.models import ProposedPlan


def canonical_json(proposed: ProposedPlan) -> str:
    """Serialize a proposed plan with sorted object keys and original resource order."""
    payload = proposed.model_dump(mode="json")
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_hash(proposed: ProposedPlan) -> str:
    """Return the lowercase SHA-256 hex digest of the canonical UTF-8 JSON."""
    return hashlib.sha256(canonical_json(proposed).encode("utf-8")).hexdigest()
