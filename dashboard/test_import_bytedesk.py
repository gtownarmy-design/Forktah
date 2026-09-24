"""taskmgmt/import_bytedesk.py against a store built here, byte by byte.

The fixture is generated rather than checked in: this repo is checked out with CRLF on
Windows, and the upstream parser requires "---\n". A checked-in store would test the
checkout's line endings instead of the importer.
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "taskmgmt" / "import_bytedesk.py"
sys.path.insert(0, str(REPO / "taskmgmt"))
import import_bytedesk as ib  # noqa: E402


def doc(data, body):
    lines = [f"{k}: {json.dumps(v)}" for k, v in data.items()]
    return "---\n" + "\n".join(lines) + "\n---\n\n" + body


EV = ".bytedesk\\task-management\\evidence\\"
TASKS = {
    "TM-001": ({"id": "TM-001", "kind": "task", "status": "open", "created": "2026-09-22T16:50:21.000Z",
                "board": "fixture", "title": "AGENT FORUM", "epic": "EP-002",
                "acceptance": [{"text": "forum stays open", "done": False}], "evidence": [],
                "commits": [], "blockedBy": [], "blocks": [], "actor": "main", "labels": ["pinned", "forum"],
                "updated": "2026-09-24T10:00:00.000Z",
                "comments": [{"author": "main", "ts": "2026-09-22T16:50:27.668Z", "text": "CLAIM TM-002 build it"},
                             {"author": "main", "ts": "2026-09-22T17:00:00.000Z", "text": "TOUCH src/app.py"},
                             {"author": "main", "ts": "2026-09-22T18:00:00.000Z", "text": "DONE TM-002\nsecond line"},
                             {"author": "worker-1", "ts": "2026-09-22T19:00:00.000Z", "text": "HANDOFF TM-003 tests left"},
                             {"author": "main", "ts": "2026-09-22T20:00:00.000Z", "text": "@lead question?"}]},
               "Coordination channel.\n"),
    "TM-002": ({"id": "TM-002", "kind": "task", "status": "done", "created": "2026-09-22T16:51:00.000Z",
                "board": "fixture", "title": "Build it", "epic": "EP-001",
                "acceptance": [{"text": "it works", "done": True, "at": "2026-09-22T17:59:00.000Z"}],
                "evidence": [EV + "TM-002-run.log", ".bytedesk/task-management/evidence/shared.md"],
                "evidenceSources": {EV + "TM-002-run.log": {"source": "C:\\tmp\\run.log",
                                                              "sha256": None, "bytes": 9,
                                                              "at": "2026-09-22T17:58:00.000Z"}},
                "commits": [], "blockedBy": [], "blocks": ["TM-003"], "actor": "main", "session": "s1",
                "branch": "main", "worktree": "C:/repo", "labels": ["ready-for-human"], "triagedBy": "human",
                "updated": "2026-09-22T18:00:00.000Z", "assignee": "claude", "closed": "2026-09-22T18:00:00.000Z",
                "comments": [{"author": "main", "ts": "2026-09-22T17:30:00.000Z", "text": "halfway"}],
                "touches": ["src\\app.py"], "links": [{"type": "relates to", "id": "TM-003"}],
                "type": "story", "priority": "high"},
               "## Goal\n\nBuild it.\n\nTrailing text with `code` and unicode: é ✓\n"),
    "TM-003": ({"id": "TM-003", "kind": "task", "status": "parked", "created": "2026-09-22T16:52:00.000Z",
                "board": "fixture", "title": "Test it", "epic": "EP-002",
                "acceptance": [{"text": "tests pass", "done": False}],
                "evidence": [".bytedesk/task-management/evidence/shared.md"], "commits": [],
                "blockedBy": ["TM-002"], "blocks": [], "actor": "main", "labels": [],
                "updated": "2026-09-22T19:00:00.000Z", "parkedReason": "waiting on hardware",
                "reopenedReason": "it came back", "goalDoc": "docs/goals/x.md",
                "dispatchFailure": {"backend": "orchestration", "reason": "no route",
                                    "at": "2026-09-22T19:02:24.752Z"}},
               ""),
}
EPICS = {
    "EP-001": ({"id": "EP-001", "kind": "epic", "status": "done", "created": "2026-09-22T16:50:19.828Z",
                "board": "fixture", "title": "General", "actor": "main", "session": "s0", "branch": "main",
                "worktree": "C:/repo", "updated": "2026-09-22T18:00:00.000Z", "closed": "2026-09-22T18:00:00.000Z",
                "plan": ".bytedesk\\task-management\\plans\\plan-1.md"}, "The first epic.\n"),
    "EP-002": ({"id": "EP-002", "kind": "epic", "status": "open", "created": "2026-09-22T16:50:20.078Z",
                "board": "fixture", "title": "Agent coordination", "actor": "main",
                "updated": "2026-09-22T16:50:20.078Z", "reopenedReason": "forum"}, ""),
}
ADRS = {
    "ADR-0001": ({"id": "ADR-0001", "kind": "adr", "status": "accepted", "created": "2026-09-22T17:00:00.000Z",
                  "board": "fixture", "title": "Use X", "epic": None, "decisionKey": "abc123",
                  "date": "2026-09-22", "updated": "2026-09-22T17:00:01.000Z"},
                 "## Context\n\nWhy.\n\n## Decision\n\nX.\n"),
}
EVENTS = [
    {"ts": "2026-09-22T16:50:12.084Z", "event": "init", "actor": "main", "root": "C:\\repo"},
    {"ts": "2026-09-22T16:50:19.828Z", "event": "create", "actor": "main", "id": "EP-001", "kind": "epic"},
    {"ts": "2026-09-22T16:51:01.354Z", "event": "claim", "actor": "main", "id": "TM-002"},
    {"ts": "2026-09-22T16:51:00.000Z", "event": "create", "actor": "main", "id": "TM-002", "kind": "task"},
    {"ts": "2026-09-22T18:00:00.000Z", "event": "done", "actor": "main", "id": "TM-002"},
    {"ts": "2026-09-22T19:00:00.000Z", "event": "subagent_stop", "actor": "main"},
]


def build_store(root):
    store = root / "repo" / ".bytedesk" / "task-management"
    for folder in ("tasks", "epics", "adrs", "evidence", "plans"):
        (store / folder).mkdir(parents=True)
    for folder, items in (("tasks", TASKS), ("epics", EPICS), ("adrs", ADRS)):
        for key, (data, body) in items.items():
            (store / folder / f"{key}-{data['title'].lower().replace(' ', '-')}.md").write_bytes(
                doc(data, body).encode("utf-8"))
    # The duplicate a crashed atomic write leaves behind. It must not be read.
    (store / "tasks" / ".tm-tmp-123-TM-002-build-it.md").write_bytes(doc(TASKS["TM-002"][0], "stale").encode())
    (store / "evidence" / "TM-002-run.log").write_bytes(b"run: ok\n\n")
    (store / "evidence" / "shared.md").write_bytes(b"shared evidence\n")
    (store / "plans" / "plan-1.md").write_bytes(b"# plan\n")
    (store / "events.jsonl").write_text("\n".join(json.dumps(e) for e in EVENTS) + "\n", encoding="utf-8")
    return store


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = build_store(root)
        self.db = root / "cc.db"
        self.evidence = root / "ccc-evidence"
        self.args = ["--store", str(self.store), "--db", str(self.db),
                     "--evidence-dir", str(self.evidence), "--evidence-ref", "C:/Users/x/ccc-evidence"]
        # A board that already has operator rows: the journal is not empty in real life.
        seed = ib.open_db(self.db)
        seed.execute("INSERT INTO journal (at,kind,agent,subject,body) VALUES ('2026-09-24T00:00:00+00:00','note','op','pre-existing','x')")
        seed.close()

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, mode, *extra):
        result = subprocess.run([sys.executable, str(SCRIPT), mode, *self.args, *extra],
                                capture_output=True, text=True, timeout=60)
        return result.returncode, json.loads(result.stdout or "{}"), result.stderr

    def query(self, sql, *params):
        db = sqlite3.connect(self.db)
        db.row_factory = sqlite3.Row
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def test_plan_counts_and_skips_temp_duplicate(self):
        rc, report, err = self.run_cli("plan")
        self.assertEqual(rc, 0, err)
        self.assertEqual(report["counts"], {"TM": 3, "EP": 2, "ADR": 1})
        self.assertEqual(report["events"], 6)
        self.assertEqual(report["forum_comments"], 5)
        self.assertIn("tasks/.tm-tmp-123-TM-002-build-it.md", [s.replace("\\", "/") for s in report["skipped"]])
        self.assertEqual(report["files"], 3)          # two evidence files (one shared) + the plan
        self.assertEqual(report["problems"], [])

    def test_apply_then_verify_is_lossless(self):
        rc, report, err = self.run_cli("apply", "--freeze-commit", "abc1234")
        self.assertEqual(rc, 0, err)
        rc, report, err = self.run_cli("verify")
        self.assertEqual(rc, 0, json.dumps(report, indent=2) + err)
        self.assertTrue(report["lossless"])
        self.assertEqual(report["checked"], 6)

    def test_keys_dates_and_children_land_as_they_were(self):
        self.run_cli("apply")
        task = self.query("SELECT * FROM tasks WHERE key='TM-002'")[0]
        self.assertEqual((task["status"], task["agent"], task["type"], task["priority"]),
                         ("done", "claude", "story", "high"))
        self.assertEqual(task["created_at"], "2026-09-22T16:51:00.000000+00:00")
        self.assertEqual(task["closed_at"], "2026-09-22T18:00:00.000000+00:00")
        self.assertEqual(self.query("SELECT key FROM epics WHERE id=?", task["epic_id"])[0][0], "EP-001")
        self.assertEqual(task["body"], TASKS["TM-002"][1])
        refs = [r[0] for r in self.query("SELECT ref FROM board_evidence WHERE entity_key='TM-002' ORDER BY id")]
        self.assertEqual(refs, ["C:/Users/x/ccc-evidence/bytedesk/evidence/TM-002-run.log",
                                "C:/Users/x/ccc-evidence/bytedesk/evidence/shared.md"])
        self.assertEqual((self.evidence / "bytedesk/evidence/shared.md").read_bytes(), b"shared evidence\n")
        self.assertEqual(self.query("SELECT link_type FROM board_links WHERE entity_key='TM-002'")[0][0], "relates")
        self.assertEqual([r[0] for r in self.query("SELECT blocked_by FROM board_deps WHERE entity_key='TM-003'")], ["TM-002"])
        self.assertEqual(self.query("SELECT plan FROM epics WHERE key='EP-001'")[0][0],
                         "C:/Users/x/ccc-evidence/bytedesk/plans/plan-1.md")
        adr = self.query("SELECT * FROM adrs WHERE key='ADR-0001'")[0]
        self.assertEqual((adr["status"], adr["decision_key"]), ("accepted", "abc123"))
        counters = {r[0]: r[1] for r in self.query("SELECT prefix,last FROM board_counters")}
        self.assertGreaterEqual(counters["TM"], 3)
        self.assertGreaterEqual(counters["EP"], 2)
        self.assertGreaterEqual(counters["ADR"], 1)

    def test_unmapped_fields_are_kept_not_dropped(self):
        self.run_cli("apply")
        detail = json.loads(self.query("SELECT detail FROM board_history WHERE entity_key='TM-003'"
                                       " AND event='imported'")[0][0])
        self.assertEqual(detail["extra"]["reopenedReason"], "it came back")
        self.assertEqual(detail["extra"]["dispatchFailure"]["reason"], "no route")
        self.assertEqual(detail["extra"]["board"], "fixture")
        failed = self.query("SELECT at FROM board_history WHERE entity_key='TM-003' AND event='dispatch-failed'")
        self.assertEqual(failed[0][0], "2026-09-22T19:02:24.752000+00:00")
        epic = json.loads(self.query("SELECT detail FROM board_history WHERE entity_key='EP-001'"
                                     " AND event='imported'")[0][0])
        self.assertEqual(epic["extra"]["worktree"], "C:/repo")

    def test_events_become_history_in_time_order(self):
        self.run_cli("apply")
        rows = self.query("SELECT at,event,entity_key,detail FROM board_history"
                          " WHERE event NOT IN ('imported','evidence-imported','dispatch-failed') ORDER BY id")
        self.assertEqual([r["event"] for r in rows], ["init", "create", "create", "claim", "done", "subagent_stop"])
        self.assertEqual([r["at"] for r in rows], sorted(r["at"] for r in rows))
        self.assertIsNone(rows[0]["entity_key"])
        self.assertEqual(json.loads(rows[3]["detail"])["raw"], json.dumps(EVENTS[2]))

    def test_forum_comments_move_to_the_journal(self):
        self.run_cli("apply")
        rows = self.query("SELECT kind,agent,subject,body FROM journal WHERE subject!='pre-existing' ORDER BY id")
        self.assertEqual([r["kind"] for r in rows], ["claim", "claim", "done", "handoff", "note"])
        self.assertEqual(rows[2]["subject"], "DONE TM-002")
        self.assertEqual(rows[2]["body"], "DONE TM-002\nsecond line")
        self.assertEqual(rows[3]["agent"], "worker-1")
        archive = (self.evidence / "TM-001" / "forum-archive.md").read_text(encoding="utf-8")
        self.assertIn("HANDOFF TM-003 tests left", archive)
        # Faithful: the forum arrives as it was; retiring it is a board action afterwards.
        self.assertEqual(self.query("SELECT status FROM tasks WHERE key='TM-001'")[0][0], "open")

    def test_second_apply_refuses_and_changes_nothing(self):
        self.run_cli("apply")
        before = self.query("SELECT COUNT(*) FROM board_history")[0][0]
        rc, report, _ = self.run_cli("apply")
        self.assertEqual(rc, 2)
        self.assertIn("already on the board", report["refused"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM board_history")[0][0], before)

    def test_verify_catches_a_changed_comment(self):
        self.run_cli("apply")
        db = sqlite3.connect(self.db)
        db.execute("UPDATE board_comments SET text='edited' WHERE entity_key='TM-002'")
        db.commit()
        db.close()
        rc, report, _ = self.run_cli("verify")
        self.assertEqual(rc, 1)
        self.assertTrue(any(p.startswith("TM-002.comments") for p in report["problems"]), report["problems"])

    def test_verify_catches_a_lost_field_and_a_bad_copy(self):
        self.run_cli("apply")
        db = sqlite3.connect(self.db)
        db.execute("UPDATE tasks SET parked_reason=NULL WHERE key='TM-003'")
        db.commit()
        db.close()
        (self.evidence / "bytedesk/evidence/shared.md").write_bytes(b"tampered\n")
        rc, report, _ = self.run_cli("verify")
        self.assertEqual(rc, 1)
        joined = "\n".join(report["problems"])
        self.assertIn("TM-003.parkedReason", joined)
        self.assertIn("differs from the source", joined)

    def test_verify_catches_a_missing_history_line(self):
        self.run_cli("apply")
        db = sqlite3.connect(self.db)
        db.execute("DELETE FROM board_history WHERE event='claim'")
        db.commit()
        db.close()
        rc, report, _ = self.run_cli("verify")
        self.assertEqual(rc, 1)
        self.assertTrue(any(p.startswith("history:") for p in report["problems"]))

    def test_missing_evidence_file_refuses_before_writing(self):
        (self.store / "evidence" / "shared.md").unlink()
        rc, report, _ = self.run_cli("apply")
        self.assertEqual(rc, 2)
        self.assertIn("evidence file missing", report["refused"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM tasks")[0][0], 0)
        self.assertFalse((self.evidence / "bytedesk").exists())

    def test_board_code_reads_the_imported_rows(self):
        """The dashboard's own readers accept what the importer wrote."""
        self.run_cli("apply")
        sys.path.insert(0, str(REPO / "dashboard"))
        import ccboard
        db = sqlite3.connect(self.db)
        db.row_factory = sqlite3.Row
        try:
            record = ccboard.entity(db, "TM-002")
            self.assertEqual(record["key"], "TM-002")
            self.assertEqual(len(record["acceptance"]), 1)
            # claim, create, done from events.jsonl; imported; two evidence-imported.
            self.assertEqual(len(ccboard.history(db, "TM-002")), 6)
            report = ccboard.doctor(db)
            self.assertEqual(report["errors"], 0, report["text"])
        finally:
            db.close()


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    print(f"passed {result.testsRun - failed}, failed {failed}")
    sys.exit(bool(failed))
