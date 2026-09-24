# ccc-board plugin

The Control Center board inside Claude Code. The board itself is the dashboard's
(`dashboard/ccboard.py`, SQLite in WSL, 127.0.0.1:8787); this plugin is its Claude-side
half. Standard library only.

| Path | What |
|---|---|
| `lib/ccc_common.py` | Board client, evidence store, WSL agentmux runner, configuration |
| `mcp/ccc_mcp.py` | stdio MCP server: 21 tools over `/api/board/*`, `/api/journal` and `agentmux` |
| `hooks/hooks.json`, `hooks/ccc_hook.py` | SessionStart summary, native-task mirror, Stop gate, park on SessionEnd |
| `skills/board/SKILL.md` | The ticket loop and when to use which tool |
| `tests/test_ccc_mcp.py` | MCP server and hooks against a stub board; no dashboard, no WSL |

## Install

```powershell
claude plugin marketplace add gtownarmy-design/Forktah
claude plugin install ccc-board@forktah
```

The dashboard must be running (`deploy/windows/ccc-keepalive` keeps it up at logon).

## Tools

Board: `board_summary`, `task_show`, `task_find`, `task_next`, `task_history`, `task_create`,
`task_status`, `task_ac_add`, `task_accept`, `task_evidence`, `task_comment`, `task_assign`,
`task_link`, `epic_create`, `epic_use`.
Coordination: `journal_write`, `journal_read`, `claim`, `release`, `claims`, `post`.

Writes are recorded as `CCC_ACTOR` (default `main`). The board's gates apply unchanged:
a ticket needs a body and a criterion to be created, and every criterion ticked plus
evidence to be closed. Refusals come back with the step that fixes them.

`task_evidence` copies the file (or writes the text) to `CCC_EVIDENCE_DIR/<KEY>/` (default
`%USERPROFILE%\ccc-evidence`) and records that path, because the board keeps a string,
never the bytes.

## Hooks

| Event | Does |
|---|---|
| SessionStart | Adds in-progress work, the next 5 unblocked tickets and the latest journal entries |
| PostToolUse `TaskCreate`/`TaskUpdate` | Mirrors native tasks: creates (or links, if the subject starts with `TM-nnn`) and starts cards. **A completed native task never closes a card** - it comments; `done` goes through the evidence gate. |
| PostToolUse `task_status` | Records which session started a card |
| Stop | Blocks once per stop while a card this session started is in progress with no evidence |
| SessionEnd | Parks this session's in-progress cards |

Every hook uses a 3 s request timeout and does nothing when the dashboard is down.

## Configuration

`CCC_DASHBOARD`, `CCC_ACTOR`, `CCC_EVIDENCE_DIR`, `CCC_EVIDENCE_REF`, `CCC_WSL_DISTRO`,
`CCC_STATE_DIR` - see `lib/ccc_common.py`.

## Test

```
python3 plugins/ccc-board/tests/test_ccc_mcp.py
```
