"""The contract shared by the local watcher and the authoritative observation writer.

Both sides import this module, so a correlation id's format, a remote run's name and an
observation's identity are defined exactly once. Two sides that each computed their own
digest would make the writer's verification arithmetic self-referential: it would check
the watcher's numbers against the watcher's numbers.

Two rules in here carry the weight:

* **Correlation identity is compared, never inferred.** ``run_name_for`` is what the
  workflow publishes as its run name, and ``is_valid_correlation_id`` is what both ends
  require before trusting one. A run counts as *this* dispatch only when its remote name
  is byte-for-byte the expected name. Run age, workflow name, branch and "some run
  exists" are deliberately not part of that decision.

* **Observation identity is evidence identity, not scan identity.** ``observed_at``
  advances on every pass, so including it would give every scan a unique identity and
  turn replay protection into a no-op. The identity is built from the source evidence
  fields instead, which is also what lets a project legitimately return to a similar
  state later: the evidence time moves, so the identity moves with it.
"""

import hashlib
import json
import os
import re
from datetime import datetime

SCHEMA_VERSION = 1

RUN_NAME_PREFIX = "project-observation-"

CORRELATION_ID_MIN_LENGTH = 12
CORRELATION_ID_MAX_LENGTH = 64
CORRELATION_ID_RE = re.compile(r"^[0-9a-f]{%d,%d}$" % (CORRELATION_ID_MIN_LENGTH, CORRELATION_ID_MAX_LENGTH))

OBSERVATION_DIGEST_LENGTH = 16
OBSERVATION_DIGEST_RE = re.compile(r"^[0-9a-f]{%d}$" % OBSERVATION_DIGEST_LENGTH)

MAX_OBSERVATIONS = 50
MAX_FACTS = 20
MAX_FACT_LENGTH = 300
MAX_REASON_LENGTH = 500
MAX_SUMMARY_LENGTH = 300
MAX_PROJECT_ID_LENGTH = 128
MAX_SOURCE_FRESHNESS_LENGTH = 64
MAX_PAYLOAD_BYTES = 256 * 1024

TOP_LEVEL_FIELDS = ("schema_version", "correlation_id", "observations")

OBSERVATION_FIELDS = (
    "project_id",
    "source_type",
    "fresh",
    "reason",
    "facts",
    "evidence_summary",
    "observed_at",
    "newest_evidence_at",
    "source_freshness",
    "observation_digest",
)

# `source_freshness` is derived by the writer from the evidence time, so its absence is
# tolerated. Every other field must be present, even when its value is empty or null:
# "absent" and "explicitly empty" are different claims about the same observation.
REQUIRED_OBSERVATION_FIELDS = tuple(f for f in OBSERVATION_FIELDS if f != "source_freshness")

ALLOWED_SOURCE_TYPES = ("repo", "local_file", "event_only")


def run_name_for(correlation_id: str) -> str:
    """The exact remote run name the workflow publishes for this correlation id."""
    return RUN_NAME_PREFIX + correlation_id


def generate_correlation_id() -> str:
    """A fresh 32-hex-character correlation id.

    Random rather than derived from the payload plus a clock reading: two identical
    payloads scanned within the same clock tick would otherwise derive the same id, and
    a colliding id would make two genuinely different dispatches indistinguishable —
    exactly the failure that exact matching exists to prevent.
    """
    return os.urandom(16).hex()


def is_valid_correlation_id(value) -> bool:
    return (
        isinstance(value, str)
        and CORRELATION_ID_MIN_LENGTH <= len(value) <= CORRELATION_ID_MAX_LENGTH
        and bool(CORRELATION_ID_RE.match(value))
    )


def is_valid_observation_digest(value) -> bool:
    return isinstance(value, str) and bool(OBSERVATION_DIGEST_RE.match(value))


def is_timestamp(value) -> bool:
    """A timezone-aware ISO-8601 timestamp.

    Naive timestamps are rejected rather than assumed to be Cairo: an ambiguous clock
    reading cannot be ordered against other evidence, which is the only thing an
    evidence timestamp is for.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None


def optional_timestamp(value) -> bool:
    """An evidence timestamp, or one of the explicitly allowed empty forms."""
    return value is None or value == "" or is_timestamp(value)


def evidence_identity_material(obs: dict) -> dict:
    """The fields that describe *what was seen*, with nothing that only describes when.

    ``observed_at`` is excluded on purpose. So is ``evidence_summary``: it is a
    rendering of ``facts`` and ``reason``, so including it would let a cosmetic change
    in wording masquerade as new evidence.
    """
    return {
        "project_id": obs.get("project_id"),
        "source_type": obs.get("source_type"),
        "fresh": obs.get("fresh"),
        "reason": obs.get("reason"),
        "facts": obs.get("facts"),
        "newest_evidence_at": obs.get("newest_evidence_at") or "",
    }


def observation_identity(obs: dict) -> str:
    """The stable, project-scoped identity of one observation's evidence."""
    blob = json.dumps(evidence_identity_material(obs), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:OBSERVATION_DIGEST_LENGTH]


def project_scoped_identity(obs) -> str:
    """Project id plus observation identity, as one deduplication key.

    Two projects whose evidence happens to hash identically must not cancel each other
    out, so the project id is part of the *key* as well as part of the hashed material.
    """
    pid = obs.get("project_id") if isinstance(obs, dict) else None
    return "{}::{}".format(pid, observation_identity(obs) if isinstance(obs, dict) else "")


def is_in_flight(status) -> bool:
    """A run that GitHub has accepted but not yet finished.

    Only the explicit terminal status is treated as not in flight. An unfamiliar status
    is still in flight, because "I do not recognise this state" is not evidence that the
    payload was lost — treating it as lost would re-send observations that may be about
    to be applied remotely.
    """
    return status != "completed"
