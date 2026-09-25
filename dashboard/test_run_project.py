#!/usr/bin/env python3
"""A run whose files live in another checkout: start, submit, review diff, approval pin.

WHAT BROKE (TM-135). Submitted paths are relative, and every consumer resolved them
against THIS checkout: `submit` hashed against $AGENTMUX_REPO (this checkout, in every
pane), `start` recorded this checkout's HEAD as the base, and the Runs view diffed and
pinned this checkout. For a research repo driven by its own orchestrator, every hash
was "missing" and the operator's review diff showed this checkout's changes instead of
the run's work - the gate would have approved the wrong bytes.

WHAT IS PINNED HERE. A run started inside another git checkout records it; submit,
the review diff, the approval pin and the drift check all use it; a run of this
checkout records nothing and reads exactly as before; `run amend` corrects a run opened
before the recording existed, and only a person may call it, only before anything is
submitted.

HERMETIC. A throwaway AGENTMUX_HOME, the journal pointed at a dead port and toasts off,
so running this outside the gate never writes to the live board or pops a notification
(TM-134 was exactly that leak).
"""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "taskmgmt"))
sys.path.insert(0, str(HERE))

HERMETIC = {"AGENTMUX_DASHBOARD": "http://127.0.0.1:9", "AGENTMUX_NO_TOAST": "1"}
PANE_VARS = ("AGENTMUX_AGENT", "AGENTMUX_ORCHESTRATOR_WARRANT", "AGENTMUX_TRUST_IDENTITY",
             "AGENTMUX_JOB", "TMUX_PANE")


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


class ProjectRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="runproject-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.project = self.tmp / "project"
        (self.project / "data").mkdir(parents=True)
        (self.project / "README.md").write_text("project\n", encoding="utf-8")
        git(self.project, "init", "-q")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "base")
        self.base = subprocess.run(["git", "-C", str(self.project), "rev-parse", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
        self._saved = {k: os.environ.get(k) for k in
                       ("AGENTMUX_HOME", *HERMETIC, *PANE_VARS)}
        os.environ["AGENTMUX_HOME"] = str(self.home)
        os.environ.update(HERMETIC)
        for key in PANE_VARS:
            os.environ.pop(key, None)
        self.addCleanup(self._restore)
        import coordination
        importlib.reload(coordination)
        import run as runmod
        self.run = importlib.reload(runmod)
        import runsview
        self.view = importlib.reload(runsview)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def cli(self, *args, cwd=None, env=None):
        environ = dict(os.environ)
        for key, value in (env or {}).items():
            if value is None:
                environ.pop(key, None)
            else:
                environ[key] = value
        return subprocess.run([sys.executable, str(REPO / "taskmgmt" / "run.py"), *args],
                              capture_output=True, text=True, env=environ,
                              cwd=str(cwd or REPO), timeout=60)

    def as_agent(self, name):
        # A worker or reviewer pane: AGENTMUX_REPO is this checkout in every pane, which
        # is what sent the hashes to the wrong tree.
        return {"AGENTMUX_AGENT": name, "AGENTMUX_TRUST_IDENTITY": "1",
                "AGENTMUX_REPO": str(REPO)}

    def start(self, cwd):
        out = self.cli("start", "project run", cwd=cwd)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def assign(self, rid):
        out = self.cli("assign", rid, "--worker", "w-a", "--reviewer", "r-a")
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def submitted(self, cwd=None):
        rid = self.start(cwd or self.project)
        job = self.assign(rid)
        (self.project / "data" / "a.csv").write_text("x,y\n1,2\n", encoding="utf-8")
        out = self.cli("submit", job, "--by", "w-a", "--files", "data/a.csv",
                       "--summary", "done", env=self.as_agent("w-a"))
        self.assertEqual(out.returncode, 0, out.stderr)
        return rid, job

    def events(self, rid):
        return [json.loads(line) for line in
                (self.home / "runs" / rid / "events.jsonl").read_text().splitlines()
                if line.strip()]

    # ── start ──────────────────────────────────────────────────────────────────
    def test_a_run_started_in_a_project_records_it_and_its_base(self):
        rid = self.start(self.project)
        start = next(e for e in self.events(rid) if e["event"] == "start")
        self.assertEqual(Path(start["repo"]).resolve(), self.project.resolve())
        self.assertEqual(start["base"], self.base)

    def test_a_run_of_this_checkout_records_nothing_new(self):
        rid = self.start(REPO)
        start = next(e for e in self.events(rid) if e["event"] == "start")
        self.assertNotIn("repo", start)
        self.assertIsNone(self.run.fold(self.run.load_events(rid))["repo"])

    # ── submit ─────────────────────────────────────────────────────────────────
    def test_submit_hashes_the_projects_file_not_missing(self):
        rid, job = self.submitted()
        sub = next(e for e in self.events(rid) if e["event"] == "submit")
        self.assertNotEqual(sub["hashes"]["data/a.csv"], "missing")

    # ── the operator's review surface ─────────────────────────────────────────
    def test_the_review_diff_is_the_projects_new_file(self):
        rid, _ = self.submitted()
        review = self.view.review_diff(rid)
        self.assertEqual(review["files"], ["data/a.csv"])
        self.assertEqual(review["base"], self.base)
        self.assertIn("new file: data/a.csv", review["diff"])
        self.assertIn("+1,2", review["diff"])

    def test_the_approval_pins_the_projects_bytes_and_sees_them_move(self):
        rid, job = self.submitted()
        out = self.cli("verdict", job, "--by", "r-a", "--pass", "--reason", "ok",
                       env=self.as_agent("r-a"))
        self.assertEqual(out.returncode, 0, out.stderr)
        record = self.view.write_approval(rid, "operator (test)", "looked", "approved")
        self.assertNotEqual(record["files"]["data/a.csv"], "missing")
        self.assertEqual(self.view.approval_drift(rid), ())
        (self.project / "data" / "a.csv").write_text("changed\n", encoding="utf-8")
        self.assertEqual(self.view.approval_drift(rid), ("data/a.csv",))

    # ── amend ──────────────────────────────────────────────────────────────────
    def test_amend_moves_a_run_opened_before_the_recording(self):
        rid = self.start(REPO)
        self.assign(rid)
        out = self.cli("amend", rid, "--repo", str(self.project), "--reason", "t")
        self.assertEqual(out.returncode, 0, out.stderr)
        state = self.run.fold(self.run.load_events(rid))
        self.assertEqual(Path(state["repo"]).resolve(), self.project.resolve())
        self.assertEqual(state["base"], self.base)

    def test_amend_is_refused_from_a_pane(self):
        rid = self.start(REPO)
        out = self.cli("amend", rid, "--repo", str(self.project),
                       env={"AGENTMUX_AGENT": "psy-orchestrator"})
        self.assertEqual(out.returncode, 2)
        self.assertIn("operator's to call", out.stderr)

    def test_amend_is_refused_once_something_was_submitted(self):
        rid, _ = self.submitted(cwd=REPO)
        out = self.cli("amend", rid, "--repo", str(self.project))
        self.assertEqual(out.returncode, 2)
        self.assertIn("already submitted", out.stderr)

    def test_amend_refuses_a_directory_that_is_not_a_checkout_top(self):
        rid = self.start(REPO)
        out = self.cli("amend", rid, "--repo", str(self.project / "data"))
        self.assertEqual(out.returncode, 2)
        self.assertIn("not the top of a git checkout", out.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
