"""Import a bytedesk task-management store into the Control Center board, losslessly.

The board mirrors the upstream record model (ccboard.py's docstring), but no API path
can land an upstream record as it was: `create()` always mints the next key and every
timestamp is `now()`. A migration that renumbers TM-060 or dates it today has not
migrated it - every commit message, branch name and evidence file that says TM-060
would point at something else. So this writes the rows directly, in one transaction,
and then proves the result.

    python3 taskmgmt/import_bytedesk.py plan   --store S --db D --evidence-dir E --evidence-ref R
    python3 taskmgmt/import_bytedesk.py apply  --store S --db D --evidence-dir E --evidence-ref R
                                               [--freeze-commit SHA]
    python3 taskmgmt/import_bytedesk.py verify --store S --db D --evidence-dir E --evidence-ref R

  --store         the upstream store: <repo>/.bytedesk/task-management
  --db            the cc.db to write (stop the dashboard first; back it up first)
  --evidence-dir  where evidence and plan files are copied, as THIS process sees it
  --evidence-ref  the same folder as the board should name it, e.g. C:/Users/you/ccc-evidence.
                  Refs are strings the server never opens, so they are written in the form
                  the people and tools reading them use.

What goes where:
  * each scalar with a board column -> that column; each list -> its child table
  * every other field (kind, board, blocks, evidenceSources, dispatchFailure,
    reopenedReason, an epic's session/branch/worktree, an ADR's date, anything new)
    -> `detail.extra` of one `imported` board_history row per entity, verbatim, so
    nothing is dropped because this file did not anticipate it
  * events.jsonl -> board_history, one row per line, in (ts, line) order, with the
    original line kept whole in `detail.line`
  * TM-001's comments -> also the journal, the forum's new home (CLAIM/TOUCH -> claim,
    DONE -> done, HANDOFF -> handoff, WITHDRAWN -> release, anything else -> note),
    plus a rendered forum-archive.md under <evidence-dir>/TM-001/
  * evidence and plan files -> copied once into <evidence-dir>/bytedesk/, refs rewritten

The importer is faithful: TM-001 and EP-002 arrive open. Retiring the forum is a board
action taken afterwards through the gate, so its history reads like every other close.

`verify` rebuilds every upstream frontmatter dict from the database and diffs it
against the file. The only differences it accepts are the declared ones: timestamp
format (same instant), rewritten evidence/plan refs, and `relates to` -> `relates`.
"""

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "dashboard"))
import ccboard  # noqa: E402
import ccstore  # noqa: E402

SOURCE = "bytedesk"
FORUM_KEY = "TM-001"
ENTITY_FILE = re.compile(r"^(TM|EP|ADR|SP|CAP)-(\d+)-.*\.md$")
DIRS = {"tasks": "TM", "epics": "EP", "adrs": "ADR", "sprints": "SP", "capabilities": "CAP"}
LINK_TYPES = {"relates to": "relates"}
JOURNAL_KINDS = {"CLAIM": "claim", "TOUCH": "claim", "DONE": "done",
                 "HANDOFF": "handoff", "WITHDRAWN": "release"}

# Upstream field -> board column, per table. Anything not listed (and not a list
# field below) is carried verbatim in the `imported` row's detail.extra.
TASK_COLUMNS = {"title": "title", "status": "status", "assignee": "agent",
                "created": "created_at", "updated": "updated_at", "closed": "closed_at",
                "body": "body", "type": "type", "priority": "priority",
                "estimate": "estimate", "rank": "rank", "actor": "actor",
                "session": "session", "branch": "branch", "worktree": "worktree",
                "blockedReason": "blocked_reason", "parkedReason": "parked_reason",
                "triagedBy": "triaged_by", "goalDoc": "goal_doc", "sprint": "sprint_key",
                "parent": "parent_key", "capability": "capability_key"}
EPIC_COLUMNS = {"title": "title", "status": "status", "created": "created_at",
                "updated": "updated_at", "closed": "closed_at", "body": "body",
                "plan": "plan", "actor": "actor"}
ADR_COLUMNS = {"title": "title", "status": "status", "body": "body", "epic": "epic_key",
               "decisionKey": "decision_key", "deciders": "deciders",
               "supersedes": "supersedes", "created": "created_at",
               "updated": "updated_at", "closed": "closed_at"}
SIMPLE_COLUMNS = {"title": "title", "status": "status", "body": "body",
                  "created": "created_at", "updated": "updated_at", "closed": "closed_at"}
TIME_FIELDS = {"created", "updated", "closed"}
TASK_LISTS = {"acceptance", "labels", "blockedBy", "evidence", "commits", "touches",
              "comments", "links"}
ALWAYS_EXTRA = {"kind", "board", "blocks", "evidenceSources"}


class ImportError_(Exception):
    """A refusal: the store or the database is not in a state this can import safely."""


# ── reading the store ────────────────────────────────────────────────────────


def parse_doc(text):
    """lib/store.mjs parseDoc, and the body as serializeDoc would write it back."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    data = {}
    for line in text[4:end].split("\n"):
        m = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
        if not m:
            continue
        key, raw = m.groups()
        try:
            data[key] = "" if raw == "" else json.loads(raw)
        except ValueError:
            data[key] = raw
    return data, re.sub(r"^\n+", "", text[end + 4:])


def load_store(store):
    store = Path(store)
    if not (store / "tasks").is_dir():
        raise ImportError_(f"not a task-management store: {store}")
    entities, skipped = {}, []
    for folder, prefix in DIRS.items():
        directory = store / folder
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            match = ENTITY_FILE.match(path.name)
            if not match:
                skipped.append(str(path.relative_to(store)))   # .tm-tmp-*, notes, templates
                continue
            if match.group(1) != prefix:
                raise ImportError_(f"{path.name} is in {folder}/")
            raw = path.read_bytes()
            data, body = parse_doc(raw.decode("utf-8"))
            key = f"{prefix}-{match.group(2)}"
            if data.get("id") != key:
                raise ImportError_(f"{path.name}: id {data.get('id')!r} does not match its name")
            if key in entities:
                raise ImportError_(f"{key} is defined twice")
            entities[key] = {"key": key, "prefix": prefix, "data": data, "body": body,
                             "file": str(path.relative_to(store)).replace("\\", "/"),
                             "sha256": hashlib.sha256(raw).hexdigest()}
    events = []
    log = store / "events.jsonl"
    if log.exists():
        for number, line in enumerate(log.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                parsed = {}
            events.append({"line": number, "raw": line, "event": parsed})
    return entities, events, skipped


def to_board_time(value):
    """An upstream ISO string in ccboard.now()'s form, or None if it is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="microseconds")


def same_instant(a, b):
    if a is None or b is None:
        return a is None and b is None
    left, right = to_board_time(a), to_board_time(b)
    return left is not None and left == right


# ── evidence and plan files ──────────────────────────────────────────────────


class Files:
    """Upstream refs are paths relative to the store's repo; the board gets a copy."""

    def __init__(self, store, evidence_dir, evidence_ref):
        self.store = Path(store)
        self.repo = self.store.parent.parent
        self.dir = Path(evidence_dir)
        self.ref = evidence_ref.rstrip("/\\")

    def source(self, ref):
        return self.repo / ref.replace("\\", "/")

    def relative(self, ref):
        path = ref.replace("\\", "/")
        for anchor in (".bytedesk/task-management/", ""):
            if path.startswith(anchor):
                return path[len(anchor):]
        return path

    def target(self, ref):
        return self.dir / SOURCE / self.relative(ref)

    def new_ref(self, ref):
        return f"{self.ref}/{SOURCE}/{self.relative(ref)}"

    def copy(self, ref):
        src, dst = self.source(ref), self.target(ref)
        if not src.is_file():
            raise ImportError_(f"evidence file missing: {ref}")
        digest = sha256_of(src)
        if dst.exists():
            if sha256_of(dst) != digest:
                raise ImportError_(f"{dst} exists with different content")
            return digest
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return digest


def sha256_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def forum_archive(entity):
    lines = [f"# {entity['key']} forum archive", "",
             f"{entity['data'].get('title', '')}", "",
             "Imported from the bytedesk task-management store. Every post is also in the",
             "Control Center journal.", ""]
    for comment in entity["data"].get("comments") or []:
        lines += [f"## {comment.get('ts')} {comment.get('author')}", "", comment.get("text", ""), ""]
    return "\n".join(lines)


# ── planning ─────────────────────────────────────────────────────────────────


def column_map(prefix):
    return {"TM": TASK_COLUMNS, "EP": EPIC_COLUMNS, "ADR": ADR_COLUMNS}.get(prefix, SIMPLE_COLUMNS)


def extras(entity):
    """Every upstream field this entity carries that no column or child table holds."""
    data = entity["data"]
    columns = column_map(entity["prefix"])
    lists = TASK_LISTS if entity["prefix"] == "TM" else set()
    skip = {"id"} | set(columns) | lists
    out = {k: v for k, v in data.items() if k not in skip}
    for name in ("created", "updated", "closed"):
        if name in data and name in columns and data[name] and to_board_time(data[name]) is None:
            out["raw_" + name] = data[name]      # an unparseable time is kept, not lost
    return out


def build_plan(entities, events, files):
    counts = {}
    for entity in entities.values():
        counts[entity["prefix"]] = counts.get(entity["prefix"], 0) + 1
    refs, problems, warnings = {}, [], []
    for entity in entities.values():
        data = entity["data"]
        for ref in data.get("evidence") or []:
            refs.setdefault(ref, set()).add(entity["key"])
        if entity["prefix"] == "EP" and data.get("plan"):
            refs.setdefault(data["plan"], set()).add(entity["key"])
    for ref in sorted(refs):
        if not files.source(ref).is_file():
            problems.append(f"evidence file missing: {ref}")
    # `blocks` is not stored: the board derives it. It must be the exact inverse.
    derived = {}
    for entity in entities.values():
        for other in entity["data"].get("blockedBy") or []:
            derived.setdefault(other, set()).add(entity["key"])
    for entity in entities.values():
        if entity["prefix"] != "TM":
            continue
        declared = set(entity["data"].get("blocks") or [])
        if declared != derived.get(entity["key"], set()):
            warnings.append(f"{entity['key']}: blocks {sorted(declared)} is not the inverse of blockedBy"
                            " (kept verbatim in detail.extra)")
    epics = {k for k, e in entities.items() if e["prefix"] == "EP"}
    for entity in entities.values():
        epic = entity["data"].get("epic")
        if entity["prefix"] == "TM" and epic and epic not in epics:
            problems.append(f"{entity['key']}: epic {epic} is not in the store")
    forum = entities.get(FORUM_KEY)
    return {"counts": counts, "events": len(events), "files": len(refs),
            "forum_comments": len((forum or {}).get("data", {}).get("comments") or []),
            "problems": problems, "warnings": warnings}


# ── applying ─────────────────────────────────────────────────────────────────


def open_db(path):
    """A connection with the board schema in place, in explicit-transaction mode."""
    db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1, 2):
        raise ImportError_(f"unsupported database version {version}")
    db.execute("BEGIN IMMEDIATE")
    for statement in ccstore.SCHEMA:
        db.execute(statement)
    ccboard.migrate(db)
    if version != 2:
        db.execute("PRAGMA user_version=2")
    db.execute("COMMIT")
    return db


def existing_keys(db):
    keys = set()
    for table in ("epics", "tasks", "adrs", "sprints", "capabilities"):
        keys |= {row[0] for row in db.execute(f"SELECT key FROM {table} WHERE key IS NOT NULL")}
    return keys


def insert(db, table, row):
    names = list(row)
    db.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
               [row[n] for n in names])
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def history_row(db, at, key, event, actor=None, session=None, detail=None):
    db.execute("INSERT INTO board_history (at,entity_key,event,actor,session,detail)"
               " VALUES (?,?,?,?,?,?)",
               (at, key, event, actor, session, json.dumps(detail) if detail is not None else None))


def scalar_row(entity, files):
    data, columns = entity["data"], column_map(entity["prefix"])
    row = {"key": entity["key"]}
    for field, column in columns.items():
        if field == "body":
            row[column] = entity["body"]
        elif field in TIME_FIELDS:
            row[column] = to_board_time(data.get(field))
        elif field == "plan" and data.get("plan"):
            row[column] = files.new_ref(data["plan"])
        elif field in data:
            value = data[field]
            row[column] = json.dumps(value) if isinstance(value, (list, dict)) else value
    return row


def apply(store, db_path, evidence_dir, evidence_ref, freeze_commit=None):
    entities, events, _ = load_store(store)
    files = Files(store, evidence_dir, evidence_ref)
    plan = build_plan(entities, events, files)
    if plan["problems"]:
        raise ImportError_("; ".join(plan["problems"]))
    db = open_db(db_path)
    clash = existing_keys(db) & set(entities)
    if clash:
        raise ImportError_(f"already on the board: {', '.join(sorted(clash)[:10])}")

    # Files first: a copy is idempotent (same bytes or a refusal), a half-written
    # transaction is not, so nothing in the database can point at a file that failed.
    digests = {}
    for entity in entities.values():
        data = entity["data"]
        for ref in list(data.get("evidence") or []) + ([data["plan"]] if entity["prefix"] == "EP" and data.get("plan") else []):
            digests[ref] = files.copy(ref)
    forum = entities.get(FORUM_KEY)
    if forum:
        archive = files.dir / FORUM_KEY / "forum-archive.md"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(forum_archive(forum), encoding="utf-8")

    stamp = ccboard.now()
    db.execute("BEGIN IMMEDIATE")
    try:
        epic_ids = {}
        for entity in sorted((e for e in entities.values() if e["prefix"] == "EP"),
                             key=lambda e: int(e["key"].split("-")[1])):
            row = scalar_row(entity, files)
            row.setdefault("status", "open")
            epic_ids[entity["key"]] = insert(db, "epics", row)
        for entity in sorted((e for e in entities.values() if e["prefix"] == "TM"),
                             key=lambda e: int(e["key"].split("-")[1])):
            data, key = entity["data"], entity["key"]
            row = scalar_row(entity, files)
            row["epic_id"] = epic_ids.get(data.get("epic"))
            row.setdefault("status", "open")
            insert(db, "tasks", row)
            updated = row.get("updated_at")
            for position, item in enumerate(data.get("acceptance") or []):
                insert(db, "board_acceptance", {"entity_key": key, "position": position,
                       "text": item.get("text", ""), "done": 1 if item.get("done") else 0,
                       "at": to_board_time(item.get("at"))})
            for label in data.get("labels") or []:
                insert(db, "board_labels", {"entity_key": key, "label": label, "at": updated})
            for other in data.get("blockedBy") or []:
                insert(db, "board_deps", {"entity_key": key, "blocked_by": other, "at": updated})
            sources = data.get("evidenceSources") or {}
            for ref in data.get("evidence") or []:
                source = sources.get(ref) or {}
                insert(db, "board_evidence", {"entity_key": key, "ref": files.new_ref(ref),
                       "at": to_board_time(source.get("at")) or updated})
            for commit in data.get("commits") or []:
                ref = commit if isinstance(commit, str) else json.dumps(commit, sort_keys=True)
                insert(db, "board_commits", {"entity_key": key, "ref": ref, "at": updated})
            for path in data.get("touches") or []:
                insert(db, "board_touches", {"entity_key": key, "path": path, "at": updated})
            for comment in data.get("comments") or []:
                insert(db, "board_comments", {"entity_key": key, "author": comment.get("author"),
                       "at": to_board_time(comment.get("ts")) or comment.get("ts") or updated,
                       "text": comment.get("text", "")})
            for link in data.get("links") or []:
                insert(db, "board_links", {"entity_key": key,
                       "link_type": LINK_TYPES.get(link.get("type"), link.get("type")),
                       "target": link.get("id"), "at": updated})
        for entity in entities.values():
            if entity["prefix"] in ("TM", "EP"):
                continue
            table = {"ADR": "adrs", "SP": "sprints", "CAP": "capabilities"}[entity["prefix"]]
            row = scalar_row(entity, files)
            row.setdefault("status", "proposed" if entity["prefix"] == "ADR" else "open")
            row.setdefault("title", entity["key"])
            insert(db, table, row)

        # The upstream log, oldest first, so history()'s id order reads as time.
        known = set(entities)
        ordered = sorted(events, key=lambda e: (to_board_time(e["event"].get("ts")) or "", e["line"]))
        for item in ordered:
            event = item["event"]
            key = event.get("id") if event.get("id") in known else None
            history_row(db, to_board_time(event.get("ts")) or stamp, key,
                        str(event.get("event") or "unknown"), event.get("actor"),
                        event.get("session"), {"source": SOURCE, "line": item["line"], "raw": item["raw"]})

        # The forum's new home. The journal has no detail column, so the range of ids
        # written here is recorded on TM-001's `imported` row for verify to find.
        journal_ids = []
        if forum:
            for comment in forum["data"].get("comments") or []:
                text = comment.get("text", "")
                first = (text.split(None, 1) or [""])[0].rstrip(":").upper()
                journal_ids.append(insert(db, "journal", {
                    "at": to_board_time(comment.get("ts")) or stamp,
                    "kind": JOURNAL_KINDS.get(first, "note"), "agent": comment.get("author"),
                    "subject": (text.splitlines() or [""])[0][:256], "body": text}))

        for entity in sorted(entities.values(), key=lambda e: e["key"]):
            data = entity["data"]
            detail = {"source": SOURCE, "file": entity["file"], "sha256": entity["sha256"],
                      "freeze": freeze_commit, "extra": extras(entity)}
            if entity["key"] == FORUM_KEY:
                detail["journal"] = journal_ids
            history_row(db, stamp, entity["key"], "imported", "migration", None, detail)
            sources = data.get("evidenceSources") or {}
            for ref in data.get("evidence") or []:
                history_row(db, stamp, entity["key"], "evidence-imported", "migration", None,
                            {"origRef": ref, "ref": files.new_ref(ref), "sha256": digests.get(ref),
                             "source": sources.get(ref)})
            failure = data.get("dispatchFailure")
            if isinstance(failure, dict):
                history_row(db, to_board_time(failure.get("at")) or stamp, entity["key"],
                            "dispatch-failed", data.get("actor"), data.get("session"), failure)

        for entity in entities.values():
            ccboard._observe(db, entity["prefix"], int(entity["key"].split("-")[1]))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    plan["applied_at"] = stamp
    return plan


# ── verifying ────────────────────────────────────────────────────────────────


def rebuild(db, entity, files):
    """The upstream dict this entity's rows describe, and the problems found on the way."""
    key, prefix, source = entity["key"], entity["prefix"], entity["data"]
    table = {"TM": "tasks", "EP": "epics", "ADR": "adrs", "SP": "sprints", "CAP": "capabilities"}[prefix]
    row = db.execute(f"SELECT * FROM {table} WHERE key=?", (key,)).fetchone()
    if row is None:
        return None, [f"{key}: not on the board"]
    imported = db.execute("SELECT detail FROM board_history WHERE entity_key=? AND event='imported'"
                          " ORDER BY id DESC LIMIT 1", (key,)).fetchone()
    detail = json.loads(imported["detail"]) if imported else {}
    out = {"id": key}
    out.update(detail.get("extra") or {})
    for name in ("created", "updated", "closed"):
        out.pop("raw_" + name, None)
    for field, column in column_map(prefix).items():
        value = row[column] if column in row.keys() else None
        if field == "body":
            continue
        if value is None and field not in source:
            continue
        if field == "plan" and value:
            out[field] = ("plan-ref", value)
            continue
        if isinstance(value, str) and isinstance(source.get(field), (list, dict)):
            value = json.loads(value)
        out[field] = value
    if prefix == "TM":
        epic = db.execute("SELECT key FROM epics WHERE id=?", (row["epic_id"],)).fetchone() if row["epic_id"] else None
        if "epic" in source or epic:
            out["epic"] = epic["key"] if epic else None
        out["acceptance"] = [{"text": r["text"], "done": bool(r["done"]), "at": r["at"]} for r in db.execute(
            "SELECT * FROM board_acceptance WHERE entity_key=? ORDER BY position", (key,))]
        out["labels"] = [r[0] for r in db.execute("SELECT label FROM board_labels WHERE entity_key=?", (key,))]
        out["blockedBy"] = [r[0] for r in db.execute("SELECT blocked_by FROM board_deps WHERE entity_key=?", (key,))]
        out["evidence"] = [("evidence-ref", r[0]) for r in db.execute(
            "SELECT ref FROM board_evidence WHERE entity_key=? ORDER BY id", (key,))]
        out["commits"] = [r[0] for r in db.execute("SELECT ref FROM board_commits WHERE entity_key=? ORDER BY id", (key,))]
        out["touches"] = [r[0] for r in db.execute("SELECT path FROM board_touches WHERE entity_key=?", (key,))]
        out["comments"] = [{"author": r["author"], "ts": r["at"], "text": r["text"]} for r in db.execute(
            "SELECT * FROM board_comments WHERE entity_key=? ORDER BY id", (key,))]
        out["links"] = [{"type": r["link_type"], "id": r["target"]} for r in db.execute(
            "SELECT * FROM board_links WHERE entity_key=? ORDER BY id", (key,))]
    return {"data": out, "body": row["body"] if "body" in row.keys() else None, "detail": detail}, []


def differs(field, want, got, files):
    """None when `got` is `want` up to a declared difference, else a description."""
    if field in TIME_FIELDS:
        return None if same_instant(want, got) else f"{want!r} != {got!r}"
    if field == "plan":
        return None if got == ("plan-ref", files.new_ref(want)) else f"{want!r} -> {got!r}"
    if field == "evidence":
        expected = [("evidence-ref", files.new_ref(ref)) for ref in want or []]
        return None if expected == got else f"{want!r} -> {got!r}"
    if field in ("labels", "blockedBy", "touches"):
        return None if sorted(want or []) == sorted(got or []) else f"{want!r} != {got!r}"
    if field == "acceptance":
        if len(want or []) != len(got):
            return f"{len(want or [])} criteria != {len(got)}"
        for a, b in zip(want or [], got):
            if a.get("text") != b["text"] or bool(a.get("done")) != b["done"] or not same_instant(a.get("at"), b["at"]):
                return f"criterion {a!r} != {b!r}"
        return None
    if field == "comments":
        if len(want or []) != len(got):
            return f"{len(want or [])} comments != {len(got)}"
        for a, b in zip(want or [], got):
            if a.get("author") != b["author"] or a.get("text") != b["text"] or not same_instant(a.get("ts"), b["ts"]):
                return f"comment {a.get('ts')} differs"
        return None
    if field == "links":
        expected = [{"type": LINK_TYPES.get(l.get("type"), l.get("type")), "id": l.get("id")} for l in want or []]
        return None if expected == got else f"{want!r} != {got!r}"
    if field == "commits":
        expected = [c if isinstance(c, str) else json.dumps(c, sort_keys=True) for c in want or []]
        return None if expected == got else f"{want!r} != {got!r}"
    return None if want == got else f"{want!r} != {got!r}"


def verify(store, db_path, evidence_dir, evidence_ref):
    entities, events, _ = load_store(store)
    files = Files(store, evidence_dir, evidence_ref)
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    problems, checked = [], 0
    try:
        for key in sorted(entities):
            entity = entities[key]
            rebuilt, found = rebuild(db, entity, files)
            problems += found
            if rebuilt is None:
                continue
            checked += 1
            source = entity["data"]
            if rebuilt["body"] is not None and rebuilt["body"] != entity["body"]:
                problems.append(f"{key}: body differs")
            if rebuilt["detail"].get("sha256") != entity["sha256"]:
                problems.append(f"{key}: imported from a different file version")
            for field in sorted(set(source) | set(rebuilt["data"])):
                if field in ("body",):
                    continue
                if field not in source:
                    empty = rebuilt["data"][field] in (None, [], {})
                    if not empty:
                        problems.append(f"{key}.{field}: on the board but not in the file")
                    continue
                if field not in rebuilt["data"]:
                    problems.append(f"{key}.{field}: in the file but not on the board")
                    continue
                why = differs(field, source[field], rebuilt["data"][field], files)
                if why:
                    problems.append(f"{key}.{field}: {why}")
            for ref in list(source.get("evidence") or []) + ([source["plan"]] if entity["prefix"] == "EP" and source.get("plan") else []):
                target = files.target(ref)
                if not target.is_file():
                    problems.append(f"{key}: copied file missing for {ref}")
                    continue
                digest = sha256_of(target)
                if digest != sha256_of(files.source(ref)):
                    problems.append(f"{key}: copy of {ref} differs from the source")
                recorded = ((source.get("evidenceSources") or {}).get(ref) or {}).get("sha256")
                if recorded and recorded != digest:
                    problems.append(f"{key}: {ref} sha256 differs from evidenceSources")

        rows = [json.loads(r[0]) for r in db.execute(
            "SELECT detail FROM board_history WHERE detail LIKE '%\"source\": \"bytedesk\"%'"
            " AND event NOT IN ('imported','evidence-imported','dispatch-failed')")]
        lines = {r.get("line"): r.get("raw") for r in rows}
        if len(rows) != len(events) or any(lines.get(e["line"]) != e["raw"] for e in events):
            problems.append(f"history: {len(rows)} imported event rows for {len(events)} events.jsonl lines")

        forum = entities.get(FORUM_KEY)
        if forum:
            detail = db.execute("SELECT detail FROM board_history WHERE entity_key=? AND event='imported'"
                                " ORDER BY id DESC LIMIT 1", (FORUM_KEY,)).fetchone()
            ids = (json.loads(detail[0]) if detail else {}).get("journal") or []
            comments = forum["data"].get("comments") or []
            journal = [db.execute("SELECT * FROM journal WHERE id=?", (i,)).fetchone() for i in ids]
            if len(journal) != len(comments) or any(
                    row is None or row["body"] != c.get("text", "") or row["agent"] != c.get("author")
                    for row, c in zip(journal, comments)):
                problems.append(f"journal: {len(journal)} forum rows for {len(comments)} TM-001 comments")
            if not (files.dir / FORUM_KEY / "forum-archive.md").is_file():
                problems.append("TM-001: forum-archive.md is missing")

        for prefix in {e["prefix"] for e in entities.values()}:
            top = max(int(k.split("-")[1]) for k, e in entities.items() if e["prefix"] == prefix)
            row = db.execute("SELECT last FROM board_counters WHERE prefix=?", (prefix,)).fetchone()
            if row is None or row[0] < top:
                problems.append(f"counter {prefix} is {row[0] if row else None}, below {prefix}-{top:03d}")
    finally:
        db.close()
    return {"checked": checked, "entities": len(entities), "events": len(events),
            "problems": problems, "lossless": not problems and checked == len(entities)}


# ── command line ─────────────────────────────────────────────────────────────


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("plan", "apply", "verify"))
    parser.add_argument("--store", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--evidence-ref", required=True)
    parser.add_argument("--freeze-commit")
    args = parser.parse_args(argv)
    try:
        if args.mode == "plan":
            entities, events, skipped = load_store(args.store)
            report = build_plan(entities, events, Files(args.store, args.evidence_dir, args.evidence_ref))
            report["skipped"] = skipped
            if Path(args.db).exists():
                db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
                try:
                    have = set()
                    for table in ("epics", "tasks", "adrs"):
                        try:
                            have |= {r[0] for r in db.execute(f"SELECT key FROM {table}")}
                        except sqlite3.Error:
                            pass
                finally:
                    db.close()
                report["clashes"] = sorted(have & set(entities))
            ok = not report["problems"] and not report.get("clashes")
        elif args.mode == "apply":
            report = apply(args.store, args.db, args.evidence_dir, args.evidence_ref, args.freeze_commit)
            ok = True
        else:
            report = verify(args.store, args.db, args.evidence_dir, args.evidence_ref)
            ok = report["lossless"]
    except ImportError_ as err:
        print(json.dumps({"refused": str(err)}, indent=2))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
