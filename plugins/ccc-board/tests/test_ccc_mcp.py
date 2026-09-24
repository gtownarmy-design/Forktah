"""The ccc-board MCP server and hooks against a REAL Control Center board.

A stub would test the plugin against what its author believed the board does. This
brings up dashboard/server.py itself on a spare port with a throwaway AGENTMUX_HOME, so
every gate and refusal is the board's own. Only agentmux (claims, posts) is faked, by a
recorder, because it lives in WSL and needs tmux.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parents[1]
SERVER = PLUGIN / "mcp" / "ccc_mcp.py"
HOOK = PLUGIN / "hooks" / "ccc_hook.py"
FAKE_AGENTMUX = """import json, os, sys
log = os.environ["FAKE_AGENTMUX_LOG"]
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["claim"] and "held" in sys.argv[2]:
    print("claim refused: held by rollcall-dev", file=sys.stderr)
    sys.exit(1)
print("ok " + " ".join(sys.argv[1:]))
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, env):
        self.proc = subprocess.Popen([sys.executable, str(SERVER)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                                          "params": params or {}}).encode() + b"\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def call(self, name, args=None):
        result = self.request("tools/call", {"name": name, "arguments": args or {}})["result"]
        text = result["content"][0]["text"]
        return result["isError"], (text if result["isError"] else json.loads(text))

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.proc.stderr.close()


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.port = free_port()
        home = root / "agentmux-home"
        home.mkdir()
        cls.server = subprocess.Popen([sys.executable, "dashboard/server.py", "--port", str(cls.port)],
                                      cwd=REPO, env={**os.environ, "AGENTMUX_HOME": str(home)},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.url = f"http://127.0.0.1:{cls.port}"
        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(cls.url + "/api/board/meta", timeout=2).read()
                break
            except OSError:
                if time.monotonic() > deadline or cls.server.poll() is not None:
                    raise RuntimeError("dashboard did not start")
                time.sleep(0.2)
        fake = root / "fake_agentmux.py"
        fake.write_text(FAKE_AGENTMUX, encoding="utf-8")
        cls.agentmux_log = root / "agentmux.log"
        cls.evidence = root / "ccc-evidence"
        cls.env = {**os.environ, "CCC_DASHBOARD": cls.url, "CCC_ACTOR": "main",
                   "CCC_EVIDENCE_DIR": str(cls.evidence), "CCC_EVIDENCE_REF": "C:/ev",
                   "CCC_STATE_DIR": str(root / "state"),
                   "CCC_AGENTMUX_CMD": json.dumps([sys.executable, str(fake)]),
                   "FAKE_AGENTMUX_LOG": str(cls.agentmux_log)}
        cls.client = Client(cls.env)
        ok, epic = cls.client.call("epic_create", {"title": "Plugin tests", "body": "fixture epic"})
        assert not ok, epic
        cls.epic = epic["key"]
        cls.client.call("epic_use", {"id": cls.epic})

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.server.terminate()
        cls.server.wait(timeout=10)
        cls.tmp.cleanup()

    def new_task(self, title, criteria=("it works",)):
        is_error, task = self.client.call("task_create", {"title": title, "body": "why: " + title,
                                                          "acceptance": list(criteria)})
        self.assertFalse(is_error, task)
        return task["key"]

    def hook(self, event, payload, env=None):
        result = subprocess.run([sys.executable, str(HOOK), event], input=json.dumps(payload),
                                capture_output=True, text=True, timeout=30, env=env or self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def show(self, key):
        is_error, task = self.client.call("task_show", {"id": key})
        self.assertFalse(is_error, task)
        return task

    # the MCP surface

    def test_tool_list(self):
        tools = self.client.request("tools/list")["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(len(tools), 21)
        self.assertTrue({"board_summary", "task_create", "task_status", "task_evidence", "journal_write",
                         "claim", "post"} <= names)
        self.assertTrue(all(t["inputSchema"]["type"] == "object" for t in tools))

    def test_full_ticket_lifecycle_through_the_gate(self):
        key = self.new_task("Lifecycle", ("first", "second"))
        self.assertFalse(self.client.call("task_status", {"id": key, "status": "in_progress"})[0])
        refused, text = self.client.call("task_status", {"id": key, "status": "done"})
        self.assertTrue(refused)
        self.assertIn("fix:", text)                      # the board's own remedy, passed through
        self.assertFalse(self.client.call("task_accept", {"id": key, "index": 1})[0])
        self.assertFalse(self.client.call("task_accept", {"id": key, "index": 2})[0])
        is_error, added = self.client.call("task_evidence", {"id": key, "text": "tests: 5 passed\n", "name": "run.log"})
        self.assertFalse(is_error, added)
        self.assertTrue(added["ref"].startswith(f"C:/ev/{key}/") and added["ref"].endswith("-run.log"))
        stored = list((self.evidence / key).iterdir())
        self.assertEqual(stored[0].read_text(encoding="utf-8"), "tests: 5 passed\n")
        is_error, done = self.client.call("task_status", {"id": key, "status": "done"})
        self.assertFalse(is_error, done)
        self.assertEqual(self.show(key)["status"], "done")

    def test_evidence_file_is_copied(self):
        key = self.new_task("Evidence copy")
        source = Path(self.tmp.name) / "report with spaces.txt"
        source.write_text("report", encoding="utf-8")
        is_error, added = self.client.call("task_evidence", {"id": key, "path": str(source)})
        self.assertFalse(is_error, added)
        self.assertIn(added["ref"], self.show(key)["evidence"])
        copies = [p for p in (self.evidence / key).iterdir() if p.name.endswith("report-with-spaces.txt")]
        self.assertEqual(copies[0].read_text(encoding="utf-8"), "report")
        self.assertTrue(self.client.call("task_evidence", {"id": key, "path": str(source) + ".missing"})[0])
        self.assertTrue(self.client.call("task_evidence", {"id": key})[0])

    def test_bad_arguments_never_reach_the_board(self):
        for name, args in (("task_create", {"title": "x", "body": "y"}),
                           ("task_create", {"title": "x", "body": "y", "acceptance": []}),
                           ("task_status", {"id": "TM-1", "status": "done"}),
                           ("task_status", {"id": "TM-001", "status": "finished"}),
                           ("task_accept", {"id": "TM-001", "index": 0}),
                           ("board_summary", {"extra": 1})):
            with self.subTest(name=name, args=args):
                self.assertTrue(self.client.call(name, args)[0])

    def test_summary_find_next_comment_assign(self):
        key = self.new_task("Findable widget")
        self.assertFalse(self.client.call("task_comment", {"id": key, "text": "a note"})[0])
        self.assertFalse(self.client.call("task_assign", {"id": key, "assignee": "claude"})[0])
        task = self.show(key)
        self.assertEqual(task["assignee"], "claude")
        self.assertEqual(task["comments"][-1]["text"], "a note")
        is_error, hits = self.client.call("task_find", {"query": "widget"})
        self.assertFalse(is_error)
        self.assertIn(key, json.dumps(hits))
        is_error, summary = self.client.call("board_summary")
        self.assertFalse(is_error)
        self.assertEqual(summary["activeEpic"], self.epic)
        self.assertFalse(self.client.call("task_history", {"id": key})[0])

    def test_journal_round_trip(self):
        self.assertFalse(self.client.call("journal_write", {"kind": "claim", "subject": "TM-999 testing the journal"})[0])
        is_error, read = self.client.call("journal_read", {"limit": 5})
        self.assertFalse(is_error)
        self.assertEqual(read["journal"][0]["subject"], "TM-999 testing the journal")
        self.assertEqual(read["journal"][0]["agent"], "main")

    def test_claims_and_posts_go_through_agentmux(self):
        self.assertFalse(self.client.call("claim", {"path": "src/app.py", "note": "editing"})[0])
        self.assertFalse(self.client.call("release", {"path": "src/app.py"})[0])
        self.assertFalse(self.client.call("post", {"to": "rollcall-dev", "kind": "request", "text": "hello", "ref": "TM-005"})[0])
        refused, text = self.client.call("claim", {"path": "held/file.py"})
        self.assertTrue(refused)
        self.assertIn("held by rollcall-dev", text)
        calls = [json.loads(l) for l in self.agentmux_log.read_text(encoding="utf-8").splitlines()]
        self.assertIn(["claim", "src/app.py", "--note", "editing"], calls)
        self.assertIn(["release", "src/app.py"], calls)
        self.assertIn(["post", "rollcall-dev", "--kind", "request", "--ref", "TM-005", "hello"], calls)

    def test_unreachable_board_is_a_sentence(self):
        client = Client({**self.env, "CCC_DASHBOARD": "http://127.0.0.1:9"})
        try:
            refused, text = client.call("board_summary")
        finally:
            client.close()
        self.assertTrue(refused)
        self.assertIn("not reachable", text)

    # hooks

    def test_session_start_summary(self):
        key = self.new_task("Shown at start")
        self.client.call("task_status", {"id": key, "status": "in_progress"})
        out = self.hook("session-start", {"session_id": "s-start"})
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("## Control Center board", text)
        self.assertIn(key, text)
        self.assertIn(self.epic, text)
        self.client.call("task_status", {"id": key, "status": "parked", "reason": "test done"})

    def test_native_task_mirror_never_closes(self):
        session = "s-mirror"
        out = self.hook("post-task", {"session_id": session, "tool_name": "TaskCreate",
                                      "tool_input": {"subject": "Mirrored work", "description": "from a todo"},
                                      "tool_response": {"task": {"id": "7", "subject": "Mirrored work"}}})
        key = out["hookSpecificOutput"]["additionalContext"].split()[-1].rstrip(".")
        self.assertEqual(self.show(key)["title"], "Mirrored work")
        self.hook("post-task", {"session_id": session, "tool_name": "TaskUpdate",
                                "tool_input": {"taskId": "7", "status": "in_progress"}})
        task = self.show(key)
        self.assertEqual((task["status"], task["session"]), ("in_progress", session))
        self.hook("post-task", {"session_id": session, "tool_name": "TaskUpdate",
                                "tool_input": {"taskId": "7", "status": "completed"}})
        task = self.show(key)
        self.assertEqual(task["status"], "in_progress")               # not closed
        self.assertIn("Close the card with evidence", task["comments"][-1]["text"])
        self.hook("session-end", {"session_id": session, "reason": "exit"})
        self.assertEqual(self.show(key)["status"], "parked")

    def test_native_task_links_an_existing_card(self):
        key = self.new_task("Already on the board")
        before = len(json.dumps(self.client.call("board_summary")[1]))
        out = self.hook("post-task", {"session_id": "s-link", "tool_name": "TaskCreate",
                                      "tool_input": {"subject": f"{key}: do the thing"},
                                      "tool_response": {"task": {"id": "1"}}})
        self.assertIn(f"linked to {key}", out["hookSpecificOutput"]["additionalContext"])
        self.hook("post-task", {"session_id": "s-link", "tool_name": "TaskUpdate",
                                "tool_input": {"taskId": "1", "status": "in_progress"}})
        self.assertEqual(self.show(key)["status"], "in_progress")
        self.client.call("task_status", {"id": key, "status": "parked", "reason": "test done"})
        self.assertGreater(before, 0)

    def test_stop_gate_and_session_binding(self):
        session = "s-stop"
        key = self.new_task("Needs evidence")
        self.client.call("task_status", {"id": key, "status": "in_progress"})
        self.hook("post-status", {"session_id": session, "tool_input": {"id": key, "status": "in_progress"}})
        self.assertEqual(self.show(key)["session"], session)
        out = self.hook("stop", {"session_id": session, "stop_hook_active": False})
        self.assertEqual(out["decision"], "block")
        self.assertIn(key, out["reason"])
        self.assertIsNone(self.hook("stop", {"session_id": session, "stop_hook_active": True}))
        self.assertIsNone(self.hook("stop", {"session_id": "another-session", "stop_hook_active": False}))
        self.client.call("task_evidence", {"id": key, "text": "proof"})
        self.assertIsNone(self.hook("stop", {"session_id": session, "stop_hook_active": False}))
        self.hook("session-end", {"session_id": session})
        self.assertEqual(self.show(key)["status"], "parked")

    def test_hooks_are_silent_and_quick_when_the_board_is_down(self):
        env = {**self.env, "CCC_DASHBOARD": "http://127.0.0.1:9"}
        started = time.monotonic()
        out = self.hook("session-start", {"session_id": "x"}, env=env)
        self.assertIn("Unavailable", out["hookSpecificOutput"]["additionalContext"])
        for event in ("stop", "session-end", "post-status"):
            self.assertIsNone(self.hook(event, {"session_id": "x", "tool_input": {"id": "TM-001", "status": "in_progress"}}, env=env))
        self.assertIsNone(self.hook("post-task", {"session_id": "x", "tool_name": "TaskCreate",
                                                  "tool_input": {"subject": "s"}}, env=env))
        self.assertLess(time.monotonic() - started, 25)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    print(f"passed {result.testsRun - failed}, failed {failed}")
    sys.exit(bool(failed))
