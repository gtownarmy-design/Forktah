---
name: board
description: Track work on the Control Center board - open a ticket before starting, move it through its states, tick acceptance criteria, attach evidence, and close it through the gate. Also the shared journal and file claims for coordinating with other agents. Use for every request that changes anything, when the user says "ticket", "board", "epic", "what's next", "what's in progress", "claim", "journal", or when a hook reports a card in progress without evidence.
---

# The Control Center board

The board is the system of record for work on this machine. It lives in the Control
Center dashboard (http://127.0.0.1:8787, Task Board view); the `ccc-board` tools
(`mcp__plugin_ccc-board_ccc-board__*`) are how a Claude session reads and writes it.
Every tool is one board request, so the board's own gates decide what is allowed, and a
refusal comes back with the step that fixes it.

## The loop for any request

1. `board_summary` or `task_find` - is there already a ticket for this? Reuse it.
2. If not, `task_create` with a body (what and why) and at least one acceptance criterion
   (how we will know it is done). It lands under the active epic; `epic_create` /
   `epic_use` when the work needs a new one.
3. `task_status` `in_progress` when you start. The hook records this session on the card.
4. `journal_write` `claim` with the ticket key and your intent, and `claim` any shared path
   before editing it. Release it when done.
5. As each criterion is met: `task_accept` (1-based index).
6. Attach proof with `task_evidence`: a file path (it is copied into the evidence store) or
   text (saved as a file there). Output of the test run, a screenshot path, a diff.
7. `task_status` `done`. The board refuses until every criterion is ticked and evidence is
   attached - read the refusal, do the step it names, and try again.
8. `journal_write` `done` (or `handoff` with what is left, and park the ticket).

Trivial one-line questions still get a ticket; create and close it in one pass.

## What the hooks do

- **Session start:** shows what is in progress, the next unblocked tickets and the latest
  journal entries.
- **Native tasks:** `TaskCreate` opens a matching card (or links to `TM-123` if the subject
  starts with a key); starting a native task starts the card. Completing a native task
  only comments - closing the card still needs evidence.
- **Stop:** a turn cannot end while a card this session started is in progress with no
  evidence. Attach evidence and finish it, or park it with a reason.
- **Session end:** this session's in-progress cards are parked.

## Coordination

- `journal_write` kinds: claim, release, note, handoff, blocked, done, plan, conflict.
- `claim` / `release` / `claims` take real, expiring locks through agentmux (as the
  orchestrator). A refused claim names the holder - do not edit that path.
- `post` queues a message for a running agentmux agent (delivered into its pane).

## If the board is unreachable

The dashboard runs in WSL (`tmux -L ccc`, kept alive at logon by `ccc-keepalive`). If the
tools report it unreachable, say so and do not pretend the ticket was written.
