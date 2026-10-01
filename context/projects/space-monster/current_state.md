# AR Space Monster

Per-project state. Written by `python -m src.project_update`; safe to edit by hand.
Level 2 of 3: this is the project, not the Staff Agent loop, and not the portfolio.

name: AR Space Monster
target_output: A child draws a monster on the printed worksheet, the phone sees it, and the monster walks in AR on the page.
current_state: Software checks pass. The drawing is captured without its guide frame and walks on the page. A physical retest on a real phone has not happened yet, and a known sealed-rectangle case is still open.
required_transition: Run the section 34 physical retest checklist on a phone with ?debug=1, then fix whatever it finds.
blocker: Needs a physical phone and a person to hold it; no automated check can decide this.
phase: validating
work_type: build
artifact_state: present
outcome_quality: unvalidated
externalization: preview_channel
accountability_source: file
scope: active
last_evidence: evt_000042 - project_evidence (2026-10-01T18:22:36+03:00)
confidence: high
evidence_source: repo
source_freshness: current
registered_at: 2026-10-01
updated_at: 2026-10-01T18:22:36+03:00

## Evidence

- evt_000028 | 2026-10-01T17:57:25+03:00 | repo | last commit ac59e35: checkpoint: adaptive child drawing extraction works on phone; 2 commits; 16 uncommitted file(s); branch master; no git remote configured; not readable from CI
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | accountability_source: unknown -> file
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | artifact_state: unknown -> present
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | blocker: unknown -> Needs a physical phone and a person to hold it; no automated check can decide this.
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | current_state: unknown -> Software checks pass. The drawing is captured without its guide frame and walks on the page. A physical retest on a real phone has not happened yet, and a known sealed-rectangle case is still open.
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | externalization: unknown -> preview_channel
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | outcome_quality: unknown -> unvalidated
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | phase: unknown -> validating
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | required_transition: unknown -> Run the section 34 physical retest checklist on a phone with ?debug=1, then fix whatever it finds.
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | scope: unknown -> active
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | target_output: unknown -> A child draws a monster on the printed worksheet, the phone sees it, and the monster walks in AR on the page.
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | work_type: unknown -> build
- evt_000038 | 2026-10-01T18:18:28+03:00 | repo | last commit ac59e35: checkpoint: adaptive child drawing extraction works on phone; 2 commits; 16 uncommitted file(s); branch master; no git remote configured; not readable from CI
- evt_000042 | 2026-10-01T18:22:36+03:00 | repo | last commit ac59e35: checkpoint: adaptive child drawing extraction works on phone; 2 commits; 16 uncommitted file(s); branch master; no git remote configured; not readable from CI

## Transitions

- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | accountability_source: unknown -> file
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | artifact_state: unknown -> present
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | blocker: unknown -> Needs a physical phone and a person to hold it; no automated check can decide this.
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | current_state: unknown -> Software checks pass. The drawing is captured without its guide frame and walks on the page. A physical retest on a real phone has not happened yet, and a known sealed-rectangle case is still open.
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | externalization: unknown -> preview_channel
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | outcome_quality: unknown -> unvalidated
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | phase: unknown -> validating
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | required_transition: unknown -> Run the section 34 physical retest checklist on a phone with ?debug=1, then fix whatever it finds.
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | scope: unknown -> active
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | target_output: unknown -> A child draws a monster on the printed worksheet, the phone sees it, and the monster walks in AR on the page.
- 2026-10-01T17:59:46+03:00 | evt_000032 | onboarding | work_type: unknown -> build
