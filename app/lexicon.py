"""DevOps wording normalizer for the built-in planner.

``normalize`` rewrites a request into the planner's own vocabulary before the
planner parses it, and records every rewrite so the reviewer can see it:

1. Unsupported resources (Redis, VMs, Kafka, ...) are reported, not rewritten.
2. Aliases from ``app/data/lexicon.json`` become the planner's word: ``rds``
   becomes ``database``, ``uat`` becomes ``test``, ``iad`` becomes ``us-east-1``.
   Longer aliases win.
3. Quantity phrasing is moved in front of the resource: ``api x3``, ``3x api``,
   ``3 replicas of the api``, and ``api with 3 instances`` become ``3 api``.
4. Remaining unknown words are compared with a short list of distinctive
   planner words using Damerau-Levenshtein distance (a swapped letter pair is
   one edit). A word of six or more letters exactly one edit from a single
   target is corrected, and the correction is recorded. A word of six or
   more letters two edits from a core resource noun is reported as a likely
   misspelling, and the planner refuses. Anything else is ignored. Edit
   distance cannot tell a typo from a real word ("staying" is one edit from
   "staging"), so the targets are chosen to have no common English word
   nearby, and ``not_typos`` lists the exceptions found by scanning a corpus.

This module only rewrites text. It does not build a plan, validate, price, or
apply policy. Nothing is corrected silently: every rewrite appears in
``Normalized.readings``.
"""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

MAX_CORRECT_EDITS = 1
MIN_CORRECT_LENGTH = 6
SUGGEST_EDITS = 2
# Six, so "datbse" (two letters missing) is refused rather than silently ignored.
# A corpus scan found no extra false refusals at six versus seven.
MIN_SUGGEST_LENGTH = 6

_REPLACEMENT = {
    "postgres": "database",
    "mysql": "mysql",
    "container": "container",
    "object_storage": "object storage",
}
_DISPLAY = {
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "container": "a container",
    "object_storage": "object storage",
}
_WORD_RE = re.compile(r"[A-Za-z]+")
_QUANTITY_UNITS = r"(?:replicas?|instances?|copies|nodes?)"


@dataclass(frozen=True)
class Alias:
    phrase: str
    replacement: str
    reading: str


@dataclass(frozen=True)
class Lexicon:
    aliases: Mapping[str, Alias]
    alias_re: re.Pattern[str] | None
    unsupported_resource_re: re.Pattern[str] | None
    unsupported_region_pattern: str
    not_typos: frozenset[str]
    known_words: frozenset[str]


@dataclass(frozen=True)
class Normalized:
    text: str
    readings: tuple[str, ...]
    misspelled: tuple[tuple[str, str], ...]
    unsupported: tuple[str, ...]


def phrase_pattern(phrases: Iterable[str]) -> str:
    """An alternation of phrases, longest first, bounded so ``us-east`` never matches ``us-east-1``."""
    ordered = sorted({phrase.lower() for phrase in phrases}, key=lambda item: (-len(item), item))
    if not ordered:
        return r"(?!x)x"
    escaped = "|".join(re.escape(phrase).replace(r"\ ", r"\s+") for phrase in ordered)
    return rf"(?<![\w-])(?:{escaped})(?![\w-])"


def load_lexicon(path: Path) -> Lexicon:
    data = json.loads(path.read_text(encoding="utf-8"))
    aliases: dict[str, Alias] = {}
    for kind, phrases in data["resources"].items():
        for phrase in phrases:
            aliases[phrase.lower()] = Alias(
                phrase, _REPLACEMENT[kind], f"as {_DISPLAY[kind]} (DevOps term)"
            )
    for environment, phrases in data["environments"].items():
        for phrase in phrases:
            aliases[phrase.lower()] = Alias(
                phrase, environment, f"as the {environment} environment"
            )
    for region, phrases in data["regions"].items():
        for phrase in phrases:
            aliases[phrase.lower()] = Alias(phrase, region, f"as region {region}")
    for size, phrases in data["sizes"].items():
        for phrase in phrases:
            aliases[phrase.lower()] = Alias(phrase, size, f"as size {size}")

    unsupported_resources = data["unsupported_resources"]
    unsupported_regions = data["unsupported_regions"]
    not_typos = frozenset(word.lower() for word in data["not_typos"])
    known_words = frozenset(
        word
        for phrase in (*aliases, *unsupported_resources, *unsupported_regions)
        for word in _WORD_RE.findall(phrase.lower())
    )
    return Lexicon(
        aliases=aliases,
        alias_re=re.compile(phrase_pattern(aliases), re.IGNORECASE) if aliases else None,
        unsupported_resource_re=re.compile(phrase_pattern(unsupported_resources), re.IGNORECASE),
        unsupported_region_pattern=phrase_pattern(unsupported_regions),
        not_typos=not_typos,
        known_words=known_words,
    )


def damerau_distance(left: str, right: str) -> int:
    """Optimal-string-alignment distance: insert, delete, substitute, or swap.

    A swapped pair of letters counts as one edit, so ``dataabse`` is one edit
    from ``database``.
    """
    if left == right:
        return 0
    rows, cols = len(left) + 1, len(right) + 1
    distance = [[0] * cols for _ in range(rows)]
    for row in range(rows):
        distance[row][0] = row
    for col in range(cols):
        distance[0][col] = col
    for row in range(1, rows):
        for col in range(1, cols):
            cost = 0 if left[row - 1] == right[col - 1] else 1
            distance[row][col] = min(
                distance[row - 1][col] + 1,
                distance[row][col - 1] + 1,
                distance[row - 1][col - 1] + cost,
            )
            if (
                row > 1
                and col > 1
                and left[row - 1] == right[col - 2]
                and left[row - 2] == right[col - 1]
            ):
                distance[row][col] = min(distance[row][col], distance[row - 2][col - 2] + 1)
    return distance[-1][-1]


def normalize(
    prompt: str,
    lexicon: Lexicon,
    *,
    typo_targets: Mapping[str, str],
    suggest_targets: Iterable[str],
    known: Iterable[str],
    resource_pattern: str,
    negation_re: re.Pattern[str],
) -> Normalized:
    """Rewrite ``prompt`` into planner vocabulary and record each rewrite.

    ``typo_targets`` maps each word a one-edit typo may be corrected to onto its
    replacement text. ``suggest_targets`` are the words a two-edit misspelling
    is refused against. ``known`` lists planner words that are never typos.
    ``resource_pattern`` matches the planner's resource phrases.
    """
    readings: list[str] = []
    unsupported = _unsupported(prompt, lexicon, negation_re)
    text = _replace_aliases(prompt, lexicon, readings)
    # Typos before quantities, so "microservise x2" binds its count.
    text, misspelled = _correct_typos(
        text,
        lexicon,
        typo_targets,
        tuple(suggest_targets),
        frozenset(known),
        readings,
    )
    text = _move_quantities(text, resource_pattern, readings)
    return Normalized(
        text=text,
        readings=tuple(dict.fromkeys(readings)),
        misspelled=tuple(misspelled),
        unsupported=unsupported,
    )


def _unsupported(prompt: str, lexicon: Lexicon, negation_re: re.Pattern[str]) -> tuple[str, ...]:
    found: list[str] = []
    if lexicon.unsupported_resource_re is None:
        return ()
    for match in lexicon.unsupported_resource_re.finditer(prompt):
        if negation_re.search(prompt[: match.start()]):
            continue
        if match.group(0).lower() not in {item.lower() for item in found}:
            found.append(match.group(0))
    return tuple(found)


def _replace_aliases(prompt: str, lexicon: Lexicon, readings: list[str]) -> str:
    if lexicon.alias_re is None:
        return prompt

    def replace(match: re.Match[str]) -> str:
        key = re.sub(r"\s+", " ", match.group(0).lower())
        alias = lexicon.aliases[key]
        readings.append(f"Read '{match.group(0)}' {alias.reading}.")
        return alias.replacement

    return lexicon.alias_re.sub(replace, prompt)


def _move_quantities(text: str, resource_pattern: str, readings: list[str]) -> str:
    def record(phrase: str, count: str, replacement: str) -> str:
        readings.append(f"Read '{phrase.strip()}' as quantity {count}.")
        return replacement

    def after_resource(match: re.Match[str]) -> str:
        # Quote only the count phrase; the resource word may already be rewritten.
        phrase = match.group(0)[len(match.group(1)) :]
        return record(phrase, match.group(2), f"{match.group(2)} {match.group(1)}")

    # "api x3" or "api with 3 replicas" -> "3 api"
    text = re.sub(
        rf"({resource_pattern})\s*[x×]\s*(\d+)\b",
        after_resource,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"({resource_pattern})\s+(?:with|running|at|on)\s+(\d+)\s+{_QUANTITY_UNITS}\b",
        after_resource,
        text,
        flags=re.IGNORECASE,
    )
    # "3x api" or "3 replicas of the api" -> "3 api"
    text = re.sub(
        r"\b(\d+)\s*[x×]\s+(?=[A-Za-z])",
        lambda m: record(m.group(0), m.group(1), f"{m.group(1)} "),
        text,
    )
    return re.sub(
        rf"\b((\d+)\s+{_QUANTITY_UNITS})\s+of\s+(?:the\s+|our\s+|an?\s+)?",
        lambda m: record(m.group(1), m.group(2), f"{m.group(2)} "),
        text,
        flags=re.IGNORECASE,
    )


def _correct_typos(
    text: str,
    lexicon: Lexicon,
    typo_targets: Mapping[str, str],
    suggest_targets: tuple[str, ...],
    planner_words: frozenset[str],
    readings: list[str],
) -> tuple[str, list[tuple[str, str]]]:
    known = lexicon.known_words | lexicon.not_typos | planner_words | set(typo_targets)
    misspelled: list[tuple[str, str]] = []
    edits: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for match in _WORD_RE.finditer(text):
        word = match.group(0).lower()
        if len(word) < MIN_CORRECT_LENGTH or word in known:
            continue
        close = sorted(
            target
            for target in typo_targets
            if damerau_distance(word, target) <= MAX_CORRECT_EDITS
        )
        fixes = {typo_targets[target] for target in close}
        if len(fixes) == 1:
            best = close[0]
            edits.append((match.start(), match.end(), typo_targets[best]))
            readings.append(f"Read '{match.group(0)}' as '{best}' (likely typo, one letter off).")
            continue
        if len(word) < MIN_SUGGEST_LENGTH or word in seen:
            continue
        near = sorted(
            target for target in suggest_targets if damerau_distance(word, target) == SUGGEST_EDITS
        )
        if near:
            seen.add(word)
            misspelled.append((match.group(0), near[0]))
    for start, end, replacement in reversed(edits):
        text = text[:start] + replacement + text[end:]
    return text, misspelled
