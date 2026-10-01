# Project Registry

The set of projects Staff Agent supervises. This file is the only place a project is
declared. Nothing is added here without real evidence of the project existing.

Deliberately a flat Markdown list, not JSON or YAML: no dependency, greppable, and the
existing state files are regex-parsed Markdown too.

Each record states honestly whether its source can actually be refreshed:

- `repo` — tracked in git, refreshed by reading that repository's state on the local machine.
- `local_file` — a status file on this machine only. **Not** readable by GitHub Actions.
- `event_only` — no automatic source. Updated only when an agent or the human reports it.

A `local_file` source cannot be refreshed by the Daily Briefing workflow, because that
workflow runs on a GitHub runner with no access to this machine. Those projects are
marked honestly rather than pretending remote freshness.

## Fields

- `id` — stable slug, used in paths and events. Never reused.
- `name` — display name.
- `aliases` — other names a human might use. Telegram routing matches on these only.
- `tracking` — `active` | `paused` | `candidate`. `candidate` means evidence exists but
  there is not enough to state operational state.
- `evidence_source` — `repo` | `local_file` | `event_only`.
- `evidence_path` — where the evidence lives, or `none`.
- `refreshable_in_actions` — whether GitHub Actions can read the source. Honest `no` is fine.
- `onboarded_at` — first date Staff Agent recorded this project.

## Projects

### staff-agent

- id: staff-agent
- name: Staff Agent
- aliases: staff agent, staffagent, chief of staff, cos, staff agent b
- tracking: active
- evidence_source: repo
- evidence_path: context/current_state.md
- refreshable_in_actions: yes
- onboarded_at: 2026-10-01

Staff Agent's own operational state. This is Level 1 and is kept **separate** from
project state: it answers "is the loop working", not "what is the work".

### space-monster

- id: space-monster
- name: AR Space Monster
- aliases: space monster, spacemonster, ar space monster, space-monster-prototype
- tracking: active
- evidence_source: repo
- evidence_path: /home/bladina/space-monster-prototype
- refreshable_in_actions: no
- onboarded_at: 2026-10-01

Git repo on this machine, 2 checkpoint commits on 2026-09-27, plus an uncommitted
Phase 2 child-drawing pipeline. Local-only: the repo has no remote, so the Daily
Briefing workflow cannot read it.

### marketing-topic-finder

- id: marketing-topic-finder
- name: Marketing Topic Finder
- aliases: marketing topic finder, topic finder, marketing agent, marketing-agent, topic-finder
- tracking: active
- evidence_source: local_file
- evidence_path: /home/bladina/Projects/marketing-topic-finder
- refreshable_in_actions: no
- onboarded_at: 2026-10-01

Code complete, credentials still placeholders, `output/` empty. Local-only, and no git
repo at all. Refreshed by reading the directory, not by a schedule.

### remotion-video

- id: remotion-video
- name: Programmatic Video (Remotion)
- aliases: programmatic video, remotion, remotion agent, remotionagent, video pipeline
- tracking: active
- evidence_source: local_file
- evidence_path: /home/bladina/Projects/BaldinaAgents/RemotionAgent
- refreshable_in_actions: no
- onboarded_at: 2026-10-01

Four rendered MP4s in `out/`, 5 compositions. Git repo initialised with staged files
but **no commits yet**. Local-only.

### comic-agent

- id: comic-agent
- name: Comic Creator Agent
- aliases: comic agent, comic creator, mcscout, nadia, comic-generator
- tracking: active
- evidence_source: local_file
- evidence_path: /home/bladina/.openclaw/workspace-comic/PROJECT_STATUS.md
- refreshable_in_actions: no
- onboarded_at: 2026-10-01

The only project with a human-written `PROJECT_STATUS.md` already. 117 strips generated
in one session on 2026-03-18; no evidence of use since. Local-only.

## Not registered, and why

Deliberately excluded so they cannot silently become noise later:

- `Projects/ComfyUI`, `Projects/eigent*`, `eigent/user_1/space_*` — upstream third-party
  checkouts, not the user's work.
- `Projects/resume_any` — git initialised, zero commits, no files. Cannot infer state.
- `.openclaw/workspace`, `.openclaw/workspace-meme` — empty git repos, zero commits.
- `staff_agent_C/Staff-agent-B` — a **duplicate working copy** of this same repository
  (same origin URL, older HEAD). Registering it would double-count Staff Agent.
- `Projects/clients/gamaltecksite`, `Projects/clients/gemy-gamal-gems` — client work with
  no status evidence found at bounded depth. Not registered rather than guessed.
- `Projects/BaldinaAgents/seo-opportunity-finder` — real project with one dated output
  from 2026-03-20, but no target/blocker evidence and no status file. Candidate, not
  onboarded. Re-evaluate when it has a stated target.
- `Projects/BaldinaAgents/memory-hub` — real and substantial, but its recent activity is
  infrastructure across many agents rather than one tracked output. Candidate.
