"""Signal-matching regressions for the state-update step.

Two real defects surfaced during the live loop:

1. `changed` was not a recognised positive signal, so the most natural confirmation of
   `changed_next_briefing` ("The recommendation changed accordingly.") produced NO
   transition at all -- the reply was consumed but silently changed nothing.

2. Matching was pure substring, so `wworked` matched `worked` and verified a checkpoint
   that the user had actually mistyped.

Matching is now word-boundary based.
"""
import pytest

import src.state_update as su


def signal_of(text):
    return su._find_signal(text)


# --- word-boundary matching -------------------------------------------------

@pytest.mark.parametrize("phrase", [
    "worked",
    "it worked",
    "worked successfully",
    "The live feedback loop worked.",
    "Reply ingestion worked",
    "it works",
    "the retrieval passed",
    "verified correctly",
])
def test_legitimate_confirmations_are_recognised(phrase):
    assert signal_of(phrase) is not None


@pytest.mark.parametrize("typo", [
    "State transition wworked",   # the real evt_000014 text
    "wworked",
    "workedd",
    "notworked",
    "preworked",
    "workedx",
])
def test_misspelt_signals_are_not_recognised(typo):
    """A typo must not verify a checkpoint."""
    assert signal_of(typo) != "worked"


# --- `changed` is a first-class confirmation ---------------------------------

@pytest.mark.parametrize("phrase", [
    "The recommendation changed accordingly.",
    "The recommendation changed.",
    "Recommendation changed as advised.",
    "The changed next briefing happened.",
    "the next briefing changed correctly",
])
def test_changed_is_a_positive_signal(phrase):
    """Regression: natural `changed_next_briefing` confirmations must verify.

    Before the fix `changed` was absent from POSITIVE_SIGNALS, so these yielded None.
    """
    assert signal_of(phrase) is not None


@pytest.mark.parametrize("phrase", [
    "nothing changed",
    "no change",
    "unchanged",
    "The recommendation did not change.",
    "not changed",
    "never changed",
])
def test_negative_change_phrases_are_not_positive(phrase):
    assert su._value_for(signal_of(phrase)) == "not_verified"


def test_changed_next_briefing_verifies_from_a_natural_confirmation():
    st = {
        "checkpoints": {"changed_next_briefing": "not_verified"},
        "aliases": {"changed_next_briefing": ["changed next briefing", "recommendation changed"]},
    }
    t = su.extract_transitions("The recommendation changed accordingly.", st)
    assert len(t) == 1
    assert t[0]["checkpoint"] == "changed_next_briefing"
    assert t[0]["to"] == "verified"


def test_misspelt_reply_does_not_verify_state_transition():
    """The exact text of the real evt_000014 reply."""
    st = {
        "checkpoints": {"state_transition": "not_verified"},
        "aliases": {"state_transition": ["state transition", "state changed"]},
    }
    assert su.extract_transitions("State transition wworked", st) == []


def test_real_reply_still_verifies_state_transition():
    st = {
        "checkpoints": {"state_transition": "not_verified"},
        "aliases": {"state_transition": ["state transition", "state changed"]},
    }
    t = su.extract_transitions("State transition worked.", st)
    assert len(t) == 1 and t[0]["to"] == "verified"


# --- alias matching is also word-boundary ------------------------------------

def test_alias_needs_a_word_boundary_too():
    st = {
        "checkpoints": {"state_transition": "not_verified"},
        "aliases": {"state_transition": ["state transition"]},
    }
    assert su.extract_transitions("The state transitional thing worked.", st) == []
    assert su.extract_transitions("The state transition worked.", st) != []