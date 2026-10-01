# Marketing Topic Finder

Per-project state. Written by `python -m src.project_update`; safe to edit by hand.
Level 2 of 3: this is the project, not the Staff Agent loop, and not the portfolio.

name: Marketing Topic Finder
target_output: A daily content brief of trending Saudi/GCC job and HR topics, scored for reach and conversion toward samimlycv.com.
current_state: The pipeline is written and has a smoke test, but it has never produced a report. output/ is empty because config.py still holds placeholder Reddit credentials and a placeholder YouTube channel id.
required_transition: Fill in real Reddit API credentials and the YouTube channel id in config.py, then run src/main.py once and keep the report it writes.
blocker: No Reddit API credentials and no YouTube channel id; config.py still contains YOUR_REDDIT_CLIENT_ID style placeholders.
phase: blocked
work_type: build
artifact_state: none
outcome_quality: unvalidated
externalization: internal_only
accountability_source: file
scope: active
last_evidence: evt_000043 - project_evidence (2026-10-01T18:22:36+03:00)
confidence: high
evidence_source: local_file
source_freshness: very_stale
registered_at: 2026-10-01
updated_at: 2026-10-01T18:22:36+03:00

## Evidence

- evt_000029 | 2026-10-01T17:57:25+03:00 | local_file | 10 files (excluding build and dependency directories); has a tests directory; output/ holds 0 file(s)
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | accountability_source: unknown -> file
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | artifact_state: unknown -> none
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | blocker: unknown -> No Reddit API credentials and no YouTube channel id; config.py still contains YOUR_REDDIT_CLIENT_ID style placeholders.
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | current_state: unknown -> The pipeline is written and has a smoke test, but it has never produced a report. output/ is empty because config.py still holds placeholder Reddit credentials and a placeholder YouTube channel id.
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | externalization: unknown -> internal_only
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | outcome_quality: unknown -> unvalidated
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | phase: unknown -> blocked
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | required_transition: unknown -> Fill in real Reddit API credentials and the YouTube channel id in config.py, then run src/main.py once and keep the report it writes.
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | scope: unknown -> active
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | target_output: unknown -> A daily content brief of trending Saudi/GCC job and HR topics, scored for reach and conversion toward samimlycv.com.
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | work_type: unknown -> build
- evt_000039 | 2026-10-01T18:18:28+03:00 | local_file | 10 files (excluding build and dependency directories); has a tests directory; output/ holds 0 file(s)
- evt_000043 | 2026-10-01T18:22:36+03:00 | local_file | 10 files (excluding build and dependency directories); has a tests directory; output/ holds 0 file(s)

## Transitions

- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | accountability_source: unknown -> file
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | artifact_state: unknown -> none
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | blocker: unknown -> No Reddit API credentials and no YouTube channel id; config.py still contains YOUR_REDDIT_CLIENT_ID style placeholders.
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | current_state: unknown -> The pipeline is written and has a smoke test, but it has never produced a report. output/ is empty because config.py still holds placeholder Reddit credentials and a placeholder YouTube channel id.
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | externalization: unknown -> internal_only
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | outcome_quality: unknown -> unvalidated
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | phase: unknown -> blocked
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | required_transition: unknown -> Fill in real Reddit API credentials and the YouTube channel id in config.py, then run src/main.py once and keep the report it writes.
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | scope: unknown -> active
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | target_output: unknown -> A daily content brief of trending Saudi/GCC job and HR topics, scored for reach and conversion toward samimlycv.com.
- 2026-10-01T17:59:56+03:00 | evt_000033 | onboarding | work_type: unknown -> build
