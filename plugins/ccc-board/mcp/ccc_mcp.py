"""MCP server for the Control Center board: tickets, the journal, and file claims.

The board lives in the Control Center dashboard (127.0.0.1:8787, SQLite in WSL). This is
its Claude-side half: every tool is one board request, so the board's own gates decide
what is allowed and its refusals come back word for word, with the verb that fixes them.

Evidence is the one thing this does beyond a request: the board stores a string, never
the bytes, so `task_evidence` copies the file under CCC_EVIDENCE_DIR/<KEY>/ first and
records that copy. Claims and messages exist only in the agentmux CLI, so those tools run
it in WSL, as the orchestrator.

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout. stdout carries protocol only.
"""

import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import ccc_common as cc  # noqa: E402

SERVER = {"name": "ccc-board", "version": "1.0.0"}
PROTOCOL = "2025-06-18"
STATUSES = ["backlog", "open", "in_progress", "blocked", "parked", "done", "deleted"]
JOURNAL_KINDS = ["claim", "release", "conflict", "note", "handoff", "blocked", "done", "plan"]
POST_KINDS = ["plan", "request", "reply", "status", "finding", "error"]


def q(value):
    return urllib.parse.quote(str(value), safe="")


def obj(props=None, required=None):
    schema = {"type": "object", "properties": props or {}, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


KEY = {"type": "string", "pattern": r"^(EP|TM|ADR|SP|CAP)-[0-9]{3,9}$"}
TEXT = {"type": "string", "minLength": 1}


def summary(_):
    snap = cc.board()
    tasks = cc.tasks_of(snap)
    counts = {}
    for t in tasks:
        counts[t.get("status")] = counts.get(t.get("status"), 0) + 1
    state = snap.get("state") or {}
    nxt = cc.api("GET", "board/next?limit=5").get("tasks", [])
    return {"activeEpic": state.get("activeEpic"), "counts": counts,
            "inProgress": [{k: t.get(k) for k in ("key", "title", "session", "actor")}
                           for t in tasks if t.get("status") == "in_progress"],
            "next": [{k: t.get(k) for k in ("key", "title", "epic")} for t in nxt],
            "dashboard": cc.dashboard()}


def evidence(args):
    ref = cc.store_evidence(args["id"], path=args.get("path"), text=args.get("text"), name=args.get("name"))
    result = cc.write("evidence", id=args["id"], ref=ref)
    return {"ref": ref, "evidence": result.get("evidence")}


def agentmux_tool(*argv):
    rc, out, err = cc.agentmux(*argv)
    if rc != 0:
        raise cc.BoardError((err or out or f"agentmux exited {rc}").strip())
    return {"output": out}


TOOLS = [
    ("board_summary", "Where the board stands: active epic, status counts, what is in progress (and in "
     "which session), and the next unblocked tasks.", obj(), summary),
    ("task_show", "One ticket (or epic, ADR) in full: body, acceptance criteria, evidence, comments, links.",
     obj({"id": KEY}, ["id"]), lambda a: cc.api("GET", f"board/entity?id={q(a['id'])}")),
    ("task_find", "Search tickets by words in their title and body.",
     obj({"query": TEXT, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ["query"]),
     lambda a: cc.api("GET", f"board/find?q={q(a['query'])}&limit={a.get('limit', 20)}")),
    ("task_next", "The next unblocked tickets, in the board's queue order.",
     obj({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
     lambda a: cc.api("GET", f"board/next?limit={a.get('limit', 5)}")),
    ("task_history", "A ticket's audit log, newest first.",
     obj({"id": KEY, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["id"]),
     lambda a: cc.api("GET", f"board/history?id={q(a['id'])}&limit={a.get('limit', 50)}")),
    ("task_create", "Open a ticket. The board requires a body and at least one acceptance criterion; "
     "it goes under the active epic unless `epic` is given.",
     obj({"title": TEXT, "body": TEXT, "acceptance": {"type": "array", "items": TEXT, "minItems": 1},
          "epic": KEY, "labels": {"type": "array", "items": TEXT},
          "priority": {"type": "string", "enum": ["highest", "high", "medium", "low", "lowest"]},
          "type": {"type": "string", "enum": ["task", "bug", "story", "spike", "chore"]}},
         ["title", "body", "acceptance"]),
     lambda a: cc.write("create", kind="task", **a)),
    ("task_status", "Move a ticket: in_progress to start it, done to close it (needs every criterion "
     "ticked and evidence), parked/blocked with a reason, open, backlog or deleted.",
     obj({"id": KEY, "status": {"type": "string", "enum": STATUSES}, "reason": TEXT}, ["id", "status"]),
     lambda a: cc.write("status", **a)),
    ("task_ac_add", "Add an acceptance criterion to a ticket.",
     obj({"id": KEY, "text": TEXT}, ["id", "text"]), lambda a: cc.write("acceptance", **a)),
    ("task_accept", "Tick (or untick) acceptance criterion `index` (1-based) on a ticket.",
     obj({"id": KEY, "index": {"type": "integer", "minimum": 1}, "done": {"type": "boolean"}}, ["id", "index"]),
     lambda a: cc.write("acceptance", id=a["id"], index=a["index"], done=a.get("done", True))),
    ("task_evidence", "Attach evidence to a ticket: a file (copied into the evidence store) or text "
     "(saved as a file there). The board records where the copy is.",
     obj({"id": KEY, "path": TEXT, "text": TEXT, "name": TEXT}, ["id"]), evidence),
    ("task_comment", "Add a comment to a ticket.",
     obj({"id": KEY, "text": TEXT}, ["id", "text"]), lambda a: cc.write("comment", **a)),
    ("task_assign", "Set who a ticket is assigned to.",
     obj({"id": KEY, "assignee": TEXT}, ["id", "assignee"]),
     lambda a: cc.write("update", id=a["id"], patch={"assignee": a["assignee"]})),
    ("task_link", "Link two tickets (relates, blocks, duplicates, ...) or add a blocker.",
     obj({"id": KEY, "type": TEXT, "target": KEY}, ["id", "type", "target"]),
     lambda a: cc.write("link", id=a["id"], type=a["type"], target=a["target"], present=True)),
    ("epic_create", "Open an epic. New tickets go under the active epic.",
     obj({"title": TEXT, "body": TEXT}, ["title"]), lambda a: cc.write("create", kind="epic", **a)),
    ("epic_use", "Make an epic the active one.",
     obj({"id": KEY}, ["id"]), lambda a: cc.write("state", name="activeEpic", value=a["id"])),
    ("journal_write", "Write to the shared journal every agent and the dashboard read: claim (I am taking "
     "this on), note, handoff (what is left), blocked, done, plan.",
     obj({"kind": {"type": "string", "enum": JOURNAL_KINDS}, "subject": TEXT, "body": {"type": "string"}},
         ["kind", "subject"]),
     lambda a: cc.api("POST", "journal", {"kind": a["kind"], "subject": a["subject"],
                                          "body": a.get("body", ""), "agent": cc.actor()})),
    ("journal_read", "The latest journal entries, newest first.",
     obj({"limit": {"type": "integer", "minimum": 1, "maximum": 200}}),
     lambda a: {"journal": (cc.api("GET", "journal").get("journal") or [])[:a.get("limit", 20)]}),
    ("claim", "Take a work lock on a path before editing something another agent could touch. "
     "Refused, with the holder's name, if someone else has it. Leases expire.",
     obj({"path": TEXT, "note": {"type": "string"}, "task": KEY}, ["path"]),
     lambda a: agentmux_tool("claim", a["path"], *(["--note", a["note"]] if a.get("note") else []),
                             *(["--task", a["task"]] if a.get("task") else []))),
    ("release", "Give a claimed path back.",
     obj({"path": TEXT}, ["path"]), lambda a: agentmux_tool("release", a["path"])),
    ("claims", "Who holds which paths right now.", obj(), lambda a: agentmux_tool("claims")),
    ("post", "Queue a message for an agentmux agent (delivered into its pane by the courier).",
     obj({"to": TEXT, "kind": {"type": "string", "enum": POST_KINDS}, "text": TEXT, "ref": TEXT},
         ["to", "text"]),
     lambda a: agentmux_tool("post", a["to"], "--kind", a.get("kind", "request"),
                             *(["--ref", a["ref"]] if a.get("ref") else []), a["text"])),
]
BY_NAME = {name: (desc, schema, fn) for name, desc, schema, fn in TOOLS}


def check_args(name, schema, args):
    props = schema["properties"]
    unknown = set(args) - set(props)
    if unknown:
        raise cc.BoardError(f"{name}: unknown argument(s) {sorted(unknown)}")
    for key in schema.get("required", []):
        if key not in args:
            raise cc.BoardError(f"{name}: missing required argument {key!r}")
    for key, value in args.items():
        spec = props[key]
        kind = spec.get("type")
        ok = {"string": isinstance(value, str), "boolean": isinstance(value, bool),
              "integer": isinstance(value, int) and not isinstance(value, bool),
              "array": isinstance(value, list)}.get(kind, True)
        if not ok:
            raise cc.BoardError(f"{name}: {key} must be a {kind}")
        if "enum" in spec and value not in spec["enum"]:
            raise cc.BoardError(f"{name}: {key} must be one of {spec['enum']}")
        if kind == "string" and spec.get("minLength") and not value.strip():
            raise cc.BoardError(f"{name}: {key} must not be empty")
        if kind == "string" and "pattern" in spec and not cc.KEY_RE.match(value):
            raise cc.BoardError(f"{name}: {key} must be a board key like TM-042")
        if kind == "array" and spec.get("minItems") and len(value) < spec["minItems"]:
            raise cc.BoardError(f"{name}: {key} needs at least {spec['minItems']} item(s)")
        if kind == "integer" and ("minimum" in spec and value < spec["minimum"]
                                  or "maximum" in spec and value > spec["maximum"]):
            raise cc.BoardError(f"{name}: {key} is out of range")
    if name == "task_evidence" and not (args.get("path") or args.get("text")):
        raise cc.BoardError("task_evidence needs a file path or some text")


def run_tool(name, args):
    if name not in BY_NAME:
        return {"content": [{"type": "text", "text": f"Unknown tool {name!r}."}], "isError": True}
    _, schema, fn = BY_NAME[name]
    try:
        check_args(name, schema, args)
        result = fn(args)
    except cc.BoardError as err:
        return {"content": [{"type": "text", "text": err.explain()}], "isError": True}
    except cc.Unreachable as err:
        return {"content": [{"type": "text", "text": str(err)}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}], "isError": False}


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                  "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS]}
    elif method == "tools/call":
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return error(mid, -32602, "arguments must be an object")
        result = run_tool(str(params.get("name")), args)
    else:
        return error(mid, -32601, f"method not found: {method}")
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main():
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            reply = error(None, -32700, "parse error")
        else:
            try:
                reply = handle(msg) if isinstance(msg, dict) else error(None, -32600, "invalid request")
            except Exception as err:  # one bad call must not take the server down
                print(f"ccc-board mcp: {type(err).__name__}: {err}", file=sys.stderr)
                reply = error(msg.get("id") if isinstance(msg, dict) else None, -32603, "internal error")
        if reply is not None:
            out.write(json.dumps(reply).encode() + b"\n")
            out.flush()


if __name__ == "__main__":
    main()
