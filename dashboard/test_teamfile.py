#!/usr/bin/env python3
"""agentmux teamfile: validation, the dry-run plan, and the spawn flags it shares.

WHAT IT GUARDS. A team file is only useful if a bad one is refused before anything is
spawned: a missing orchestrator, an agent with no definition, a reviewer on the same
model as its worker, a worker reviewing itself. Each refusal below is paired with the
valid file it was derived from, so "it refused" cannot be "it was broken".

It also pins spawn-args, the piece `agentmux orchestrator start` now uses. Before
TM-128 the orchestrator was spawned with its CLI alone - no persona, no posture - so
it came up without the rules its persona carries.

No tmux, no agents, no network: definitions are written to a temp project, and CLI
lookup is stubbed.
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "taskmgmt"))

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

import teamfile  # noqa: E402

DEFS = {
    "t-orch": ("claude", "lead", "unrestricted"),
    "t-writer": ("claude", "worker", "unrestricted"),
    "t-scout": ("grok", "worker", "workspace-write"),
    "t-check": ("grok", "reviewer", "unrestricted"),
}

VALID = {
    "version": 1,
    "team": "t-team",
    "project": None,          # filled in per test
    "charter": "CHARTER.md",
    "epic": "EP-123",
    "courier": True,
    "idle_minutes": 0,
    "orchestrator": {"agent": "t-orch", "request": "do the thing", "hours": 6},
    "members": [{"agent": "t-writer"}, {"agent": "t-scout"}, {"agent": "t-check"}],
    "review": {"t-writer": "t-check", "t-scout": "t-writer"},
}


def which_all(cli):
    return f"/usr/bin/{cli}"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="teamfile-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # A private AGENTMUX_HOME, so the operator's global agents cannot leak in or collide.
        self.home = self.tmp / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"AGENTMUX_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = self.tmp / "project"
        agents = self.project / ".agentmux" / "agents"
        agents.mkdir(parents=True)
        (self.project / "CHARTER.md").write_text("# charter\n", encoding="utf-8")
        for name, (cli, role, posture) in DEFS.items():
            (agents / f"{name}.md").write_text(textwrap.dedent(f"""\
                ---
                name: {name}
                description: test agent {name}
                cli: {cli}
                posture: {posture}
                role: {role}
                worktree: none
                max_instances: 1
                ---

                Persona of {name}.
                """), encoding="utf-8")

    def doc(self, **changes):
        import copy
        d = copy.deepcopy(VALID)
        d["project"] = str(self.project)
        for key, value in changes.items():
            if value is None:
                d.pop(key, None)
            else:
                d[key] = value
        return d

    def check(self, doc, which=which_all):
        return teamfile.validate(self.tmp / "team.yaml", doc=doc, which=which)


class Validation(Base):
    def test_the_valid_file_passes(self):
        team, errors, warnings = self.check(self.doc())
        self.assertEqual(errors, [])
        self.assertEqual(team.orchestrator.name, "t-orch")
        self.assertEqual([m.spec.name for m in team.members], ["t-writer", "t-scout", "t-check"])
        writer = next(m for m in team.members if m.spec.name == "t-writer")
        self.assertEqual(writer.reviewer, "t-check")
        self.assertEqual(writer.reviews, ["t-scout"])
        self.assertEqual(writer.reports_to, "t-orch")

    def test_no_orchestrator_is_refused(self):
        _, errors, _ = self.check(self.doc(orchestrator=None))
        self.assertTrue(any("orchestrator is required" in e for e in errors), errors)

    def test_an_agent_with_no_definition_is_refused(self):
        _, errors, _ = self.check(self.doc(members=[{"agent": "t-writer"}, {"agent": "t-ghost"}],
                                           review={}))
        self.assertTrue(any("no usable definition 't-ghost'" in e for e in errors), errors)

    def test_a_same_cli_review_pair_is_refused(self):
        _, errors, _ = self.check(self.doc(review={"t-scout": "t-check"}))
        self.assertTrue(any("both run grok" in e for e in errors), errors)

    def test_a_worker_reviewing_itself_is_refused(self):
        _, errors, _ = self.check(self.doc(review={"t-writer": "t-writer"}))
        self.assertTrue(any("cannot review its own work" in e for e in errors), errors)

    def test_the_orchestrator_cannot_be_a_reviewer(self):
        _, errors, _ = self.check(self.doc(review={"t-scout": "t-orch"}))
        self.assertTrue(any("cannot verdict" in e for e in errors), errors)

    def test_a_cli_missing_from_path_is_refused(self):
        _, errors, _ = self.check(self.doc(), which=lambda cli: None if cli == "grok" else "/x")
        self.assertTrue(any("'grok', which is not on PATH" in e for e in errors), errors)

    def test_an_unknown_key_is_refused_not_ignored(self):
        _, errors, _ = self.check(self.doc(clis={"t-writer": "codex"}))
        self.assertTrue(any("unknown key 'clis'" in e for e in errors), errors)

    def test_an_unknown_kickoff_placeholder_is_refused(self):
        _, errors, _ = self.check(self.doc(kickoff="hello {agent}, meet {nobody}"))
        self.assertTrue(any("unknown placeholder {nobody}" in e for e in errors), errors)

    def test_a_member_twice_is_refused(self):
        _, errors, _ = self.check(self.doc(members=[{"agent": "t-writer"}, {"agent": "t-writer"}],
                                           review={}))
        self.assertTrue(any("appears twice" in e for e in errors), errors)

    def test_an_unreviewed_worker_is_a_warning(self):
        _, errors, warnings = self.check(self.doc(review={"t-writer": "t-check"}))
        self.assertEqual(errors, [])
        self.assertTrue(any("t-scout is a worker with no reviewer" in w for w in warnings))


class SpawnFlags(Base):
    def spec(self, name):
        import agentdefs
        return agentdefs.resolve(name, str(self.project))

    def test_flags_carry_the_definition(self):
        flags = teamfile.spawn_flags(self.spec("t-scout"))
        self.assertEqual(flags[:8], ["--cli", "grok", "--agentdef", "t-scout",
                                     "--posture", "workspace-write", "--role", "worker"])

    def test_the_role_can_be_overridden_for_the_orchestrator(self):
        flags = teamfile.spawn_flags(self.spec("t-writer"), role="lead")
        self.assertEqual(flags[flags.index("--role") + 1], "lead")

    def test_no_bypass_brakes_unrestricted(self):
        with mock.patch.dict(os.environ, {"AGENTMUX_NO_BYPASS": "1"}):
            flags = teamfile.spawn_flags(self.spec("t-writer"))
        self.assertEqual(flags[flags.index("--posture") + 1], "workspace-write")

    def test_spawn_args_writes_the_persona_and_names_it(self):
        persona = self.tmp / "p.persona"
        out = subprocess.run(
            [sys.executable, str(REPO / "taskmgmt" / "teamfile.py"), "spawn-args", "t-orch",
             "--project", str(self.project), "--role", "lead", "--persona-out", str(persona)],
            capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(out.returncode, 0, out.stderr)
        flags = out.stdout.split("\0")[:-1]
        self.assertIn("--persona-file", flags)
        self.assertEqual(flags[flags.index("--persona-file") + 1], str(persona))
        self.assertEqual(flags[flags.index("--posture") + 1], "unrestricted")
        self.assertEqual(flags[flags.index("--agentdef") + 1], "t-orch")
        self.assertIn("Persona of t-orch.", persona.read_text(encoding="utf-8"))

    def test_spawn_args_refuses_an_unknown_definition(self):
        out = subprocess.run(
            [sys.executable, str(REPO / "taskmgmt" / "teamfile.py"), "spawn-args", "t-ghost",
             "--project", str(self.project)],
            capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.stdout, "")
        self.assertIn("no usable definition 't-ghost'", out.stderr)


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class DryRun(Base):
    def test_the_plan_spawns_members_with_personas_then_the_orchestrator(self):
        import yaml
        path = self.tmp / "team.yaml"
        path.write_text(yaml.safe_dump(self.doc()), encoding="utf-8")
        buf = io.StringIO()
        with mock.patch("shutil.which", which_all), contextlib.redirect_stdout(buf):
            rc = teamfile.main(["up", str(path), "--dry-run"])
        self.assertEqual(rc, 0)
        lines = buf.getvalue().splitlines()
        self.assertEqual(lines[0], "export AGENTMUX_IDLE_MINUTES=0")
        self.assertTrue(lines[1].endswith("courier start"))
        spawns = [line for line in lines if " spawn " in line]
        self.assertEqual(len(spawns), 3)
        for line in spawns:
            self.assertIn("--persona-file", line)
            self.assertIn(f"--cwd {self.project}", line)
        orch = next(line for line in lines if " orchestrator start " in line)
        self.assertIn(f"--cwd {self.project}", orch)
        self.assertIn("--agent t-orch", orch)
        self.assertLess(lines.index(spawns[-1]), lines.index(orch),
                        "members come up before the orchestrator starts briefing them")
        kick = next(line for line in lines if line.startswith("# kickoff t-scout"))
        self.assertIn("$AGENTMUX_PERSONA_FILE", kick)
        self.assertIn("reviewed by t-writer", kick)

    def test_a_bad_file_exits_2_before_anything_runs(self):
        import yaml
        path = self.tmp / "team.yaml"
        path.write_text(yaml.safe_dump(self.doc(review={"t-writer": "t-writer"})),
                        encoding="utf-8")
        err = io.StringIO()
        with mock.patch("shutil.which", which_all), contextlib.redirect_stderr(err), \
                mock.patch.object(teamfile, "run") as runner:
            rc = teamfile.main(["up", str(path)])
        self.assertEqual(rc, 2)
        runner.assert_not_called()
        self.assertIn("cannot review its own work", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=1)
