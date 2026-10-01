"""Route a Telegram reply to exactly one project, or to none.

The whole point of this module is what it *refuses* to do. A reply is routed only
when it names exactly one project by an explicit alias, on word boundaries. Anything
else — no alias, two aliases, an alias inside a quoted string about a third project —
is stored as evidence and left unrouted. Silence is a valid outcome; guessing a
project and writing it into a state file is not.

Nothing here is model-generated. That keeps the 10-minute Fast Mode loop silent and
deterministic, exactly like ``src/state_update.py``.
"""

import re

# Routing is deliberately narrow. A routed reply may only move `phase`, because that
# is the one field a quoted positive/negative sentence genuinely supports. Deriving a
# target, a blocker or a required transition from prose would be invention, so those
# fields only ever change through `python -m src.project_update onboard`.

# Phrases that mean "still not done". Mirrors the negative-signal idea in
# src/state_update.py: a negative signal outranks a positive one in the same sentence.
NEGATIVE_SIGNALS = (
    "not tested",
    "not verified",
    "not working",
    "not done",
    "not yet",
    "not shipped",
    "not deployed",
    "not published",
    "not fixed",
    "still not",
    "still broken",
    "still needs",
    "still waiting",
    "never",
    "failed",
    "fails",
    "blocked",
    "stuck",
    "waiting on",
    "wrong",
    "broken",
    "regressed",
)

POSITIVE_SIGNALS = (
    "verified",
    "tested",
    "worked",
    "works",
    "passed",
    "passes",
    "fixed",
    "shipped",
    "deployed",
    "published",
    "done",
    "complete",
    "completed",
    "confirmed",
    "green",
    "released",
    "live",
)

PHASE_BY_SIGNAL = {
    "positive": "validating",
    "negative": "blocked",
}

_SENTENCE_SPLIT = re.compile(r"[.!?\n]+")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def _contains_word(haystack: str, phrase: str) -> bool:
    """Word-boundary match, for the same reason src/state_update.py uses one.

    Substring matching would let "remotionless" route to the video project.
    """
    pattern = r"(?<!\w)" + re.escape(phrase) + r"(?!\w)"
    return re.search(pattern, haystack) is not None


def find_aliases(text: str, alias_index: dict[str, list[str]]) -> list[str]:
    """Every alias mentioned in the text, longest first.

    Longest-first matters: "space monster" and "monster" both appearing must resolve to
    the same project rather than looking like two candidates.
    """
    lowered = text.lower()
    hits = [alias for alias in alias_index if _contains_word(lowered, alias)]
    return sorted(hits, key=len, reverse=True)


def resolve_project(text: str, alias_index: dict[str, list[str]]) -> dict:
    """Decide which project a reply is about.

    Returns ``{"project_id": str | None, "reason": str, "matched_alias": str | None,
    "candidates": list[str]}``. ``project_id`` is None whenever the reply is not
    unambiguously about one project.
    """
    hits = find_aliases(text, alias_index)
    if not hits:
        return {
            "project_id": None,
            "reason": "no explicit project alias in the reply",
            "matched_alias": None,
            "candidates": [],
        }

    owners: set[str] = set()
    for alias in hits:
        owners.update(alias_index[alias])

    if len(owners) > 1:
        return {
            "project_id": None,
            "reason": f"ambiguous: aliases {hits} match more than one project {sorted(owners)}",
            "matched_alias": None,
            "candidates": sorted(owners),
        }

    matched = next((a for a in hits if alias_index[a] and alias_index[a][0] in owners), None)
    return {
        "project_id": sorted(owners)[0],
        "reason": f"explicit alias '{matched}'",
        "matched_alias": matched,
        "candidates": sorted(owners),
    }


def _find_signal(sentence: str) -> str | None:
    lowered = sentence.lower()
    negative = [s for s in NEGATIVE_SIGNALS if _contains_word(lowered, s)]
    if negative:
        return negative[0]
    positive = [s for s in POSITIVE_SIGNALS if _contains_word(lowered, s)]
    return positive[0] if positive else None


def derive_field_updates(text: str, project_id: str, alias_index: dict[str, list[str]]) -> list[dict]:
    """Field changes a routed reply supports, with the quote that justified each.

    Only the sentences that actually name the project may change a field, so
    "the space monster worked, but the comic agent is still blocked" updates the video
    project and leaves the comic project's state untouched.
    """
    updates: list[dict] = []
    phrases = [
        phrase
        for alias, owners in alias_index.items()
        if project_id in owners
        for phrase in (alias,)
    ]
    for sentence in split_sentences(text):
        lowered = sentence.lower()
        hit = next((p for p in phrases if _contains_word(lowered, p)), None)
        if not hit:
            continue
        signal = _find_signal(sentence)
        if signal is None:
            continue
        polarity = "negative" if signal in NEGATIVE_SIGNALS else "positive"
        updates.append(
            {
                "project_id": project_id,
                "field": "phase",
                "value": PHASE_BY_SIGNAL[polarity],
                "signal": signal,
                "matched_alias": hit,
                "quote": sentence,
                "polarity": polarity,
            }
        )
    return updates
