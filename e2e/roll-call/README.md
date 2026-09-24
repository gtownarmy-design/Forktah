# Agent Roll Call

A tiny web app that shows the four agents of a coordinated run, one card each: avatar, name,
role and provider. No dependencies, no build step, no external requests.

Requires Node.js 18 or newer (tested on 22).

## Run

```sh
node server/index.js            # http://localhost:4173
PORT=8080 node server/index.js  # any other port
```

Open the printed URL. The page fetches `/api/agents` and renders the cards. It works down to
360px wide and follows the system light/dark setting (`prefers-color-scheme`).

## Test

```sh
node --test
```

Run from the repository root. The tests start the server on a random port and cover
`/api/health`, `/api/agents`, static files, 404s and path-traversal rejection (plain and
URL-encoded `..`, encoded slashes and backslashes).

## API

| Route | Response |
|---|---|
| `GET /api/health` | `{ "ok": true }` |
| `GET /api/agents` | Array of 4 `{ id, name, role, provider, avatar }`; `avatar` is `/avatars/<id>.svg` |
| anything else | Static file from `web/`, `404` if missing, `403` on `..` traversal, `400` on malformed paths |

## Layout

```
server/index.js   HTTP server (exports createServer for tests)
web/              index.html, app.js, style.css
web/avatars/      <id>.svg + manifest.json, drawn by the imager agent
test/             node:test suite
```

If an avatar file is missing, the card shows a neutral lettered placeholder in the same
square frame, so the layout does not shift.

## Run it as an agentmux team

The app is also the product of an end-to-end coordination exercise: four agents rebuild it from
`BRIEF.md`. The agents are defined in the repository roster, `.agentmux/agents/rollcall-*.md`:

| Definition | Role | CLI | Owns |
|---|---|---|---|
| `rollcall-lead` | lead | claude | the brief and the definition of done; writes no code |
| `rollcall-dev` | worker | claude | `server/`, `web/*.html,js,css`, `test/`, `README.md` |
| `rollcall-imager` | worker | grok | `web/avatars/` |
| `rollcall-reviewer` | reviewer | grok | the gate: `run verdict`; never edits |

From the repository root, inside WSL, in a checkout where the app files have been removed
(everything but `BRIEF.md`) so the agents have something to build:

```sh
agentmux courier start                          # delivers `agentmux post` messages
R=$(agentmux run start "Rebuild e2e/roll-call from BRIEF.md")
python3 e2e/roll-call/spawn_team.py             # resolves each definition, then spawns it
J1=$(agentmux run assign "$R" --worker rollcall-dev    --reviewer rollcall-reviewer --brief "server, page, tests, README per BRIEF.md")
J2=$(agentmux run assign "$R" --worker rollcall-imager --reviewer rollcall-reviewer --brief "four SVG avatars + manifest per BRIEF.md")
# answer first-run prompts (below), then send each agent its kickoff (below)
agentmux run status "$R"      # one line per job
agentmux run complete "$R"    # refuses until the reviewer has passed every job
agentmux run teardown "$R"
```

What the first real run (2026-09-24, run `0cb4e1`) showed:

- **Spawn through `spawn_team.py`, not `spawn --agentdef` alone.** `--agentdef` only records which
  definition a pane came from. The script passes the definition's cli, posture, role and persona,
  as the dashboard's hire does.
- **Claude's first run in a fresh agent config asks where the folder is trusted,** and the
  default is *No, exit*. Answer with `agentmux key <name> Down`, check with `agentmux read`, then
  `agentmux key <name> Enter`. Grok shows a data-sharing banner that needs no answer.
- **Nothing tells an agent its persona or job.** The persona reaches the pane only as the file
  named by `$AGENTMUX_PERSONA_FILE`, `run assign` records a brief without delivering it, and
  `$AGENTMUX_JOB` is unset for hand-spawned agents. The first `agentmux send` to each agent says:
  read `$AGENTMUX_PERSONA_FILE` and `BRIEF.md`, this is your job id, and this is who you report to.
- **Killing the last agent stops the courier.** Start it again before the next kickoff.
- **`run teardown` closes only the agents named in the run's jobs** (workers and reviewers). The
  lead is in no job, so stop it yourself: `agentmux kill rollcall-lead`.

In that run the lead briefed both workers through the courier. The reviewer passed both jobs on
the first try, and `run complete` accepted 2/2. The rebuilt app passed 6 of its own
`node --test` checks, with four valid SVG avatars. The commit is on the Forktah branch
`rollcall-run-20260924`.

Coordination uses agentmux's own primitives: `claim`/`release` before editing, `post --kind
request|reply|finding|status` between agents, `journal` for decisions, `run submit` from a worker
and `run verdict --pass|--fail` from the reviewer.
