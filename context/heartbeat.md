## How to evaluate current work and decide whether to intervene

This file is **policy**, not state. Current targets, blockers and checkpoints live in
`current_state.md`. Do not put changing project state here.

## Intervene when

1. `target_output` is known (not Unknown), **and**
2. the next transition named in `current_state.md` is blocked, **and**
3. you can name the smallest action that unblocks it.

All three must hold. If any one is missing, stay silent.

## Stay silent when

- `target_output` is Unknown. Record Unknown, or ask one question. Do not invent a target.
- Every checkpoint for the current target is verified and the target is met.
- The only thing you can say is that something looks untidy: a prototype is unpublished,
  a project is old, a tool is new, the architecture is complex, no journal entry exists.
- The evidence is a hypothesis rather than a fact.

Silence is a valid outcome. Returning "no intervention needed" is not a failure.

## Always check first

Before recommending anything, read `current_state.md` and check:

- Is this action already listed as `verified`? If so, do not recommend it again.
- What is `next_action`? Prefer it over anything of your own invention.
- What is `last_evidence` and how old is it? Stale evidence is not current evidence.

## Escalating a blocker

Escalate only when the blocker is repeated across **at least two** briefings against a
**known** target output, and the previously recommended action produced no new evidence.
Repetition alone is not a reason to escalate; unchanged state under a known target is.

## One intervention per briefing

Surface one main intervention. Do not dump every project into every briefing. If several
projects are live, pick the one holding the current `target_output` and ignore the rest.

## Question discipline

Ask at most one question per briefing, and only when the answer would change the next
action. If it would not, omit the question entirely.
