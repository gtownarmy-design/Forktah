"""Spawn the Agent Roll Call team from the repository roster.

`agentmux spawn --agentdef NAME` records which definition a pane came from; it does
not read the definition. The dashboard's hire path (dashboard/boardteams.py) resolves
the definition and passes its cli, posture, role, model, tools and persona itself.
This does the same for the hand-run exercise.

Each agent's persona reaches its pane only as the file named by $AGENTMUX_PERSONA_FILE,
so the first message the orchestrator sends each agent must tell it to read that file.

Run from anywhere inside WSL:
    python3 e2e/roll-call/spawn_team.py [--cwd DIR] [--dry-run]
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "taskmgmt"))
import agentdefs  # noqa: E402
# The flags live in one place now, shared with `agentmux teamfile` and
# `agentmux orchestrator start`. A standing team would use a team file instead
# (docs/TEAMFILE.md); this script stays as the recorded 2026-09-24 procedure.
from teamfile import spawn_argv  # noqa: E402

TEAM = ("rollcall-lead", "rollcall-dev", "rollcall-imager", "rollcall-reviewer")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cwd", default=str(REPO / "e2e" / "roll-call"),
                        help="working directory for every agent (default: this folder)")
    parser.add_argument("--dry-run", action="store_true", help="print the spawn commands only")
    args = parser.parse_args()

    specs = []
    for name in TEAM:
        spec = agentdefs.resolve(name, str(REPO))
        if spec is None:
            sys.exit(f"{name}: no valid definition in {REPO / '.agentmux/agents'}")
        specs.append(spec)

    for spec in specs:
        argv = spawn_argv(spec, args.cwd)
        if args.dry_run:
            print(" ".join(argv + ["--persona-file", "<persona>"]))
            continue
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".persona") as persona:
            persona.write(spec.persona)
            persona.flush()
            rc = subprocess.run(argv + ["--persona-file", persona.name]).returncode
        if rc:
            sys.exit(f"{spec.name}: agentmux spawn failed (exit {rc})")
        print(f"spawned {spec.name} ({spec.cli}, {spec.role})")


if __name__ == "__main__":
    main()
