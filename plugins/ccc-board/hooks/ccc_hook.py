"""Claude Code hooks for the Control Center board.

    session-start  what is in progress, what is next, what the journal last said
    post-task      mirror a native TaskCreate/TaskUpdate onto the board
    post-status    remember which session started a card through task_status
    stop           do not end a turn while this session's started card has no evidence
    session-end    park this session's started cards, so nothing looks active when it is not

Every hook is bounded (3 s per request) and does nothing when the dashboard is down: a
missing board must never stop a session from starting, working or ending.

Mirroring never closes a card. A native task marked completed adds a comment; `done`
still goes through the board's gate, which wants every criterion ticked and evidence.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import ccc_common as cc  # noqa: E402

TIMEOUT = 3


def emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def context(event, text):
    emit({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})


def get(path):
    return cc.api("GET", path, timeout=TIMEOUT)


def post(op, **fields):
    body = {k: v for k, v in fields.items() if v is not None}
    body.setdefault("actor", cc.actor())
    return cc.api("POST", f"board/{op}", body, timeout=TIMEOUT)


# ── session-start ────────────────────────────────────────────────────────────


def session_start(payload):
    try:
        snap = cc.board(TIMEOUT)
        nxt = get("board/next?limit=5").get("tasks", [])
        journal = (get("journal").get("journal") or [])[:5]
    except (cc.Unreachable, cc.BoardError) as err:
        context("SessionStart", f"## Control Center board\nUnavailable: {err}")
        return
    tasks = cc.tasks_of(snap)
    active = (snap.get("state") or {}).get("activeEpic")
    lines = ["## Control Center board", f"Dashboard: {cc.dashboard()}  ·  active epic: {active or '(none)'}"]
    working = [t for t in tasks if t.get("status") == "in_progress"]
    if working:
        lines.append("In progress:")
        lines += [f"- {t['key']} {t.get('title', '')}" + (f"  (session {str(t.get('session'))[:8]})" if t.get("session") else "")
                  for t in working[:10]]
    if nxt:
        lines.append("Next unblocked:")
        lines += [f"- {t.get('key')} {t.get('title', '')}" for t in nxt]
    if journal:
        lines.append("Journal (latest):")
        lines += [f"- {j.get('at', '')[:16]} {j.get('kind')} {j.get('agent') or ''}: {j.get('subject', '')[:100]}"
                  for j in journal]
    lines.append("Tools: mcp__plugin_ccc-board_ccc-board__* (board_summary, task_create, task_status, "
                 "task_accept, task_evidence, journal_write, claim, ...).")
    context("SessionStart", "\n".join(lines))


# ── mirroring native tasks ───────────────────────────────────────────────────


def mirror_file(session):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session or "unknown")[:80]
    return cc.state_dir() / f"mirror-{safe}.json"


def load_map(session):
    try:
        return json.loads(mirror_file(session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_map(session, mapping):
    mirror_file(session).write_text(json.dumps(mapping, indent=1), encoding="utf-8")


def native_id(tool_input, tool_response):
    for source in (tool_input, tool_response):
        if not isinstance(source, dict):
            continue
        for key in ("taskId", "id", "task_id"):
            if source.get(key) not in (None, ""):
                return str(source[key])
        task = source.get("task")
        if isinstance(task, dict) and task.get("id") not in (None, ""):
            return str(task["id"])
    return None


def post_task(payload):
    session = payload.get("session_id")
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    response = payload.get("tool_response")
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            response = {"text": response}
    mapping = load_map(session)
    try:
        if tool == "TaskCreate":
            nid = native_id({}, response) or native_id(tool_input, {})
            subject = str(tool_input.get("subject") or tool_input.get("title") or "").strip()
            if not subject:
                return
            linked = cc.LEADING_KEY.match(subject)
            if linked:
                try:
                    get(f"board/entity?id={linked.group(1)}")
                    key, created = linked.group(1), False
                except cc.BoardError:
                    linked = None
            if not linked:
                body = str(tool_input.get("description") or subject)
                card = post("create", kind="task", title=subject[:200], body=body[:8000],
                            mirror=True, session=session)
                key, created = card.get("key") or card.get("id"), True
            if nid:
                mapping[nid] = {"key": key, "created": created}
                save_map(session, mapping)
            context("PostToolUse", f"Board: native task {'mirrored as' if created else 'linked to'} {key}.")
        elif tool == "TaskUpdate":
            nid = native_id(tool_input, response)
            entry = mapping.get(nid or "")
            if not entry:
                return
            key, status = entry["key"], tool_input.get("status")
            if status == "in_progress":
                post("status", id=key, status="in_progress", mirror=True, session=session)
                post("update", id=key, patch={"session": session}, session=session)
            elif status == "completed":
                post("comment", id=key, text=f"Native task completed in session {str(session)[:8]}. "
                     "Close the card with evidence: task_evidence, then task_status done.")
                context("PostToolUse", f"Board: {key} is not closed by a native task. Attach evidence and "
                        "tick its criteria, then set it done.")
            elif status == "deleted":
                if entry.get("created"):
                    post("status", id=key, status="deleted", mirror=True, session=session)
                else:
                    post("comment", id=key, text="A native task linked to this card was deleted.")
    except (cc.Unreachable, cc.BoardError):
        return


def post_status(payload):
    """task_status in_progress through the MCP tool: record which session did it."""
    tool_input = payload.get("tool_input") or {}
    if tool_input.get("status") != "in_progress" or not tool_input.get("id"):
        return
    try:
        post("update", id=tool_input["id"], patch={"session": payload.get("session_id")},
             session=payload.get("session_id"))
    except (cc.Unreachable, cc.BoardError):
        return


# ── stop and session end ─────────────────────────────────────────────────────


def stop(payload):
    if payload.get("stop_hook_active"):
        return          # one reminder per stop, never a loop
    try:
        working = cc.session_tasks(payload.get("session_id"), timeout=TIMEOUT)
    except (cc.Unreachable, cc.BoardError):
        return
    bare = [t for t in working if not t.get("evidence")]
    if not bare:
        return
    keys = ", ".join(t["key"] for t in bare)
    emit({"decision": "block", "reason":
          f"Control Center: {keys} is in progress in this session with no evidence. Attach evidence "
          "(task_evidence), tick its criteria (task_accept) and set it done, or park it with a reason "
          "(task_status parked) if the work continues later."})


def session_end(payload):
    session = payload.get("session_id")
    try:
        for task in cc.session_tasks(session, timeout=TIMEOUT):
            post("status", id=task["key"], status="parked",
                 reason=f"session {str(session)[:8]} ended ({payload.get('reason') or 'exit'})", session=session)
    except (cc.Unreachable, cc.BoardError):
        return


HANDLERS = {"session-start": session_start, "post-task": post_task, "post-status": post_status,
            "stop": stop, "session-end": session_end}


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    handler = HANDLERS.get(event)
    if handler is None:
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    try:
        handler(payload if isinstance(payload, dict) else {})
    except Exception as err:  # a hook must never break the session it serves
        print(f"ccc-board hook {event}: {type(err).__name__}: {err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
