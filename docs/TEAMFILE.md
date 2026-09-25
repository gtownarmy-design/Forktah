# Team files: `agentmux teamfile`

A team file describes a **standing team**: the same few agents working a project across
many cards, with one orchestrator and fixed review pairs. One command brings the team
up and another takes it down. It exists because the hand procedure (see
`e2e/roll-call/README.md`) kept hitting the same traps:

- Nothing delivered an agent's persona to it.
- The idle watchdog closed a reviewer that was only waiting for work.
- The orchestrator came up without its rules.

The launcher is `taskmgmt/teamfile.py`, and `dashboard/test_teamfile.py` tests it.

```sh
agentmux teamfile validate team.yaml            # check only; exit 2 on any error
agentmux teamfile up team.yaml --dry-run        # print every command up would run
agentmux teamfile up team.yaml                  # courier, members, orchestrator, kickoffs
agentmux teamfile kickoff team.yaml --agent X   # resend a kickoff (after a prompt)
agentmux teamfile status team.yaml
agentmux teamfile down team.yaml                # orchestrator stopped + warrant revoked, members killed
```

## What the file owns, and what it does not

Each agent's `cli`, `posture`, `model`, `role` and persona live in its **definition**,
`.agentmux/agents/<name>.md` (see `taskmgmt/agentdefs.py`). Definitions are looked up
in the project directory first, then in the global scope (`~/.agentmux/agents`).

The team file only arranges definitions into a team: who orchestrates, who is a member,
who reviews whom, and what the kickoff says. It has no `cli` field, because a second
place to state an agent's CLI would be a second source of truth.

## Format

```yaml
version: 1
team: psy-roadmap                  # [a-z][a-z0-9-]*
project: /abs/path/to/project      # every agent's working directory
charter: CHARTER.md                # optional, relative to project
epic: EP-035                       # optional, used in kickoff text
courier: true                      # start the message courier (default true)
idle_minutes: 0                    # optional; 0 = never idle-close these agents

orchestrator:
  agent: psy-orchestrator          # a definition, normally role: lead; gets the warrant
  hours: 12                        # warrant lifetime (default 8, max 72)
  request: >-                      # what it is for; its first message
    Drive EP-035 to done in one run...

members:
  - agent: psy-analyst
    reports_to: psy-orchestrator   # optional; defaults to the orchestrator
  - agent: psy-scout
  - agent: psy-verifier

review:                            # worker: reviewer
  psy-analyst: psy-verifier        # claude work checked by grok
  psy-scout: psy-analyst           # grok work checked by claude

kickoff: >-                        # optional template
  You are {agent} on team {team}. First: cat "$AGENTMUX_PERSONA_FILE" and follow it ...
```

Kickoff placeholders: `{agent} {team} {project} {charter} {epic} {reports_to}
{reviews} {reviewed_by}`. An unknown placeholder is an error, not a blank.

`validate` refuses the file when:
- a key is unknown, or `version` is not 1;
- there is no orchestrator or no request;
- an agent has no usable definition, or runs a CLI that is not on PATH;
- a member appears twice, or `reports_to` names someone outside the team;
- a review pair uses the same CLI, a worker reviews itself, or a reviewer is not a member
  (the orchestrator cannot verdict).

A worker with no reviewer is only a warning.

## What `up` does, in order

1. `agentmux courier start` (unless `courier: false`).
2. It spawns each member with its definition's flags and persona (`--persona-file`). The
   `idle_minutes` value is exported as `AGENTMUX_IDLE_MINUTES`, so members spawned with
   `0` carry a `run/<name>.noidle` marker. A watchdog someone else starts cannot close
   them.
3. `agentmux orchestrator start --agent <o> --cwd <project> --hours <h> --request ...`.
   The definition is resolved from the project and the flags are built by the same
   `spawn-args` code, so the orchestrator gets its persona and posture too.
4. It sends each member its kickoff and waits until the pane is quiet. The orchestrator's
   kickoff goes **last**, and only once every member is briefed, so it never assigns work
   to an agent that has not read its persona.

A kickoff is **never forced**. When a pane shows a prompt, `up` exits 3 and names the pane
and the command that resends the kickoff. The usual prompt is Claude's folder-trust
dialog on a first run in a new folder. Look at it with `agentmux read <name>`, answer it
with `agentmux key <name> Down` / `Enter`, then run `teamfile kickoff`. Trusting the folder
once in WSL Claude (`cd <project> && claude`, answer "Yes, I trust this folder", exit)
removes the prompt for every later spawn.

Agents that are already running are left alone, so `up` can be run again safely.

## Runs of a project outside this checkout

`run start` records the project's git checkout when it is not this one. Submissions are
hashed there, and the Runs view diffs and pins approvals there (TM-135). A run opened
before that recording existed can be moved once, before anything is submitted:
`agentmux run amend <run> --repo <project>`. Only the operator can do that, from outside
any pane.

## Worked example

`/mnt/c/Users/brent/research/psychedelic-approval/team.yaml` is the first team to use this:
2 Claude and 2 Grok sessions, on EP-035. Its `runbook/RUNBOOK.md` records how the first
run went, and the bugs it found are EP-036 (TM-127 to TM-135).
