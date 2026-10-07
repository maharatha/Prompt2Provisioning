import json
import re
from pathlib import Path

import pytest

from app.lexicon import damerau_distance, load_lexicon, phrase_pattern
from app.planner import MockPlanner, _normalize

LEXICON_PATH = Path(__file__).resolve().parents[1] / "app" / "data" / "lexicon.json"


@pytest.mark.parametrize(
    ("left", "right", "distance"),
    [
        ("database", "database", 0),
        ("dataabse", "database", 1),  # swapped pair is one edit
        ("databse", "database", 1),
        ("postgress", "postgres", 1),
        ("contaner", "container", 1),
        ("dataabes", "database", 2),
        ("staying", "staging", 1),
        ("", "abc", 3),
    ],
)
def test_damerau_distance(left: str, right: str, distance: int) -> None:
    assert damerau_distance(left, right) == distance
    assert damerau_distance(right, left) == distance


# Real English words one or two edits from a planner word. A 25,000-word scan
# found these; each must pass through untouched, never corrected or refused.
ENGLISH_LOOKALIKES = [
    "staying", "stating", "starting", "standing", "stacking", "backed", "fronted",
    "brackets", "projection", "protection", "reduction", "introduction", "productive",
    "duplication", "replication", "allocations", "publications", "complication",
    "reproduction", "prepared", "sandboxed", "containing", "contained", "dataset",
    "worked", "working", "there", "smell", "stall", "orange", "virginica", "postage",
]


@pytest.mark.parametrize("word", ENGLISH_LOOKALIKES)
def test_english_lookalikes_are_never_corrected_or_refused(word: str) -> None:
    normalized = _normalize(f"Two web containers {word} the release")
    assert normalized.misspelled == ()
    assert not any("likely typo" in reading for reading in normalized.readings)
    assert word in normalized.text


def test_alias_never_matches_inside_a_longer_token() -> None:
    regex = re.compile(phrase_pattern(["us-east", "api"]), re.IGNORECASE)
    assert regex.search("us-east-1") is None
    assert regex.search("rapid") is None
    assert regex.search("an api in us-east").group(0) == "api"


def test_lexicon_file_is_consistent() -> None:
    data = json.loads(LEXICON_PATH.read_text(encoding="utf-8"))
    load_lexicon(LEXICON_PATH)
    assert set(data["resources"]) == {"postgres", "mysql", "container", "object_storage"}
    assert set(data["environments"]) == {"dev", "test", "prod"}
    assert set(data["regions"]) == {"us-east-1", "us-west-2", "eastus2"}

    aliases = [
        phrase.lower()
        for section in ("resources", "environments", "regions", "sizes")
        for phrases in data[section].values()
        for phrase in phrases
    ]
    assert len(aliases) == len(set(aliases)), "an alias maps to two meanings"
    unsupported = {phrase.lower() for phrase in data["unsupported_resources"] + data["unsupported_regions"]}
    assert not unsupported & set(aliases), "a phrase is both supported and unsupported"


def test_every_rewrite_is_reported_once() -> None:
    planner = MockPlanner()
    readings = planner.explain("two pods and two pods and an rds in iad")
    assert readings == [
        "Read 'pods' as a container (DevOps term).",
        "Read 'rds' as PostgreSQL (DevOps term).",
        "Read 'iad' as region us-east-1.",
    ]


def test_negated_unsupported_resource_is_not_refused() -> None:
    payload = json.loads(MockPlanner().generate("two containers, no redis"))
    assert "interpretation_error" not in payload
    assert payload["resources"][0]["quantity"] == 2
