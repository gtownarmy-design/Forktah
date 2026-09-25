#!/usr/bin/env python3
"""agentmux teamfile: bring an agent team up and down from one YAML file.

    agentmux teamfile validate team.yaml
    agentmux teamfile up       team.yaml [--dry-run]
    agentmux teamfile status   team.yaml
    agentmux teamfile kickoff  team.yaml [--agent NAME ...]
    agentmux teamfile down     team.yaml [--dry-run]

WHY A FILE. A team used to be either a card's roster on the board (hired one card at a
time) or a bespoke script (e2e/roll-call/spawn_team.py). A standing team for a project -
the same four agents across many cards, with a fixed review pairing - had no home, so
every session rebuilt it by hand and rediscovered the same three traps: the persona is
never delivered, the idle watchdog closes a reviewer that is merely waiting, and the
orchestrator comes up without its rules. The file states the team once; `up` avoids the
traps every time.

WHAT THE FILE OWNS, AND WHAT IT DOES NOT. Each agent's cli, posture, model and persona
live in its definition, .agentmux/agents/<name>.md, resolved from the project directory
first and then the global scope (taskmgmt/agentdefs.py). The team file only arranges
definitions: who orchestrates, who is a member, who reviews whom, what the kickoff says.
A cli written in the team file would be a second source of truth, so there is no field
for it.

    version: 1
    team: psy-roadmap                 # [a-z][a-z0-9-]*
    project: /abs/path                # the agents' working directory
    charter: CHARTER.md               # optional, relative to project
    epic: EP-035                      # optional, for the kickoff text
    courier: true                     # start the message courier (default true)
    idle_minutes: 0                   # optional: AGENTMUX_IDLE_MINUTES for these spawns
    orchestrator:
      agent: psy-orchestrator         # a definition; gets the warrant
      request: "..."                  # what it is for; sent as its first message
      hours: 12                       # warrant lifetime (default 8)
    members:
      - agent: psy-analyst
        reports_to: psy-orchestrator  # optional; default the orchestrator
    review:                           # worker: reviewer, different CLIs, never self
      psy-analyst: psy-verifier
    kickoff: "..."                    # optional template; placeholders below

Kickoff placeholders: {agent} {team} {project} {charter} {epic} {reports_to}
{reviews} {reviewed_by}.

`spawn-args` is the piece `agentmux orchestrator start` shares with this file, so an
orchestrator started by hand gets exactly the flags a team member does.
"""
import argparse
import os
import re
import shlex
import shutil
import string
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

TASKMGMT = Path(__file__).resolve().parent
REPO = TASKMGMT.parent
sys.path.insert(0, str(TASKMGMT))
import agentdefs  # noqa: E402

AGENTMUX = os.environ.get("AGENTMUX_BIN", "agentmux")
SOCKET = "agentmux"                       # agentmux.sh: SOCKET="agentmux"
TEAM_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
EPIC_RE = re.compile(r"EP-[0-9]{3,9}")
TOP_KEYS = {"version", "team", "project", "charter", "epic", "courier", "idle_minutes",
            "orchestrator", "members", "review", "kickoff"}
ORCH_KEYS = {"agent", "request", "hours"}
MEMBER_KEYS = {"agent", "reports_to"}
PLACEHOLDERS = {"agent", "team", "project", "charter", "epic", "reports_to", "reviews",
                "reviewed_by"}
AGENT_CLIS = {"claude", "codex", "grok"}  # a missing one is an error; `shell` needs nothing
DEFAULT_KICKOFF = (
    "You are {agent} on team {team}. First: cat \"$AGENTMUX_PERSONA_FILE\" and follow it. "
    "Then read {charter} in {project}. You report to {reports_to}; your work is reviewed by "
    "{reviewed_by}; you review: {reviews}. Wait for a job from {reports_to}. "
    "Reply \"ready\" in one line when you have read both.")
ORCH_KICKOFF = (
    "You are {agent}, the orchestrator of team {team}. First: cat \"$AGENTMUX_PERSONA_FILE\" "
    "and follow it; your warrant is in place. Team members (all up and briefed): {members}. "
    "Review pairs (worker -> reviewer): {pairs}. Your request: {request}")


class TeamError(Exception):
    pass


@dataclass
class Member:
    spec: object
    reports_to: str
    reviewer: str = ""
    reviews: list = field(default_factory=list)


@dataclass
class Team:
    path: Path
    name: str
    project: Path
    charter: str
    epic: str
    courier: bool
    idle_minutes: object
    orchestrator: object
    request: str
    hours: float
    members: list
    review: dict
    kickoff: str

    def agents(self):
        return [self.orchestrator.name] + [m.spec.name for m in self.members]


# ── loading and validation ─────────────────────────────────────────────────────

def load_yaml(path):
    try:
        import yaml
    except ImportError:
        raise TeamError("PyYAML is not installed: sudo apt install python3-yaml, "
                        "or pip install pyyaml")
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except OSError as exc:
        raise TeamError(f"cannot read {path}: {exc}")
    except yaml.YAMLError as exc:
        raise TeamError(f"{path} is not valid YAML: {exc}")


def validate(path, doc=None, which=None):
    """Return (team or None, errors, warnings). Never raises for a bad file."""
    which = which or shutil.which
    errors, warnings = [], []
    path = Path(path)
    try:
        doc = load_yaml(path) if doc is None else doc
    except TeamError as exc:
        return None, [str(exc)], warnings
    if not isinstance(doc, dict):
        return None, ["the team file must be a mapping (key: value at the top level)"], warnings

    for key in sorted(set(doc) - TOP_KEYS):
        errors.append(f"unknown key {key!r} (known: {', '.join(sorted(TOP_KEYS))})")
    if doc.get("version") != 1:
        errors.append("version must be 1")
    name = doc.get("team")
    if not isinstance(name, str) or not TEAM_RE.fullmatch(name):
        errors.append("team must be a lower-case name like psy-roadmap")
        name = str(name)

    project = doc.get("project")
    if not isinstance(project, str) or not project:
        errors.append("project is required: the agents' working directory")
        project_dir = path.resolve().parent
    else:
        project_dir = Path(project)
        if not project_dir.is_absolute():
            project_dir = (path.resolve().parent / project_dir)
        if not project_dir.is_dir():
            errors.append(f"project {project_dir} is not a directory")
    charter = doc.get("charter") or ""
    if charter and not (project_dir / charter).is_file():
        errors.append(f"charter {charter} not found in {project_dir}")
    epic = doc.get("epic") or ""
    if epic and not (isinstance(epic, str) and EPIC_RE.fullmatch(epic)):
        errors.append(f"epic {epic!r} must look like EP-035")
    courier = doc.get("courier", True)
    if not isinstance(courier, bool):
        errors.append("courier must be true or false")
    idle = doc.get("idle_minutes")
    if idle is not None and (isinstance(idle, bool) or not isinstance(idle, int) or idle < 0):
        errors.append("idle_minutes must be a whole number >= 0 (0 disables the timeout)")

    specs, problems = agentdefs.load_all(str(project_dir))

    def resolve(agent, where):
        if not isinstance(agent, str) or not agentdefs.NAME_RE.fullmatch(agent):
            errors.append(f"{where}: agent must be a definition name, got {agent!r}")
            return None
        spec = specs.get(agent)
        if spec is None:
            reasons = [p["error"] for p in problems if Path(p["path"]).stem == agent]
            errors.append(f"{where}: no usable definition {agent!r} in "
                          f"{project_dir}/.agentmux/agents or the global scope"
                          + (f" ({'; '.join(reasons)})" if reasons else ""))
            return None
        if spec.cli in AGENT_CLIS and not which(spec.cli):
            errors.append(f"{where}: {agent} runs {spec.cli!r}, which is not on PATH")
        return spec

    orch = doc.get("orchestrator")
    orch_spec, request, hours = None, "", 8.0
    if not isinstance(orch, dict):
        errors.append("orchestrator is required: agent, request, and optionally hours")
    else:
        for key in sorted(set(orch) - ORCH_KEYS):
            errors.append(f"orchestrator: unknown key {key!r}")
        orch_spec = resolve(orch.get("agent"), "orchestrator")
        request = orch.get("request") or ""
        if not isinstance(request, str) or not request.strip():
            errors.append("orchestrator: request is required - say what it should do")
        hours = orch.get("hours", 8)
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= 72:
            errors.append("orchestrator: hours must be a number between 0 and 72")
            hours = 8
        if orch_spec is not None and orch_spec.role != "lead":
            warnings.append(f"orchestrator {orch_spec.name} has role {orch_spec.role!r}; "
                            f"'lead' keeps dispatch's collect/pool from treating it as a worker")

    members = []
    raw_members = doc.get("members")
    if not isinstance(raw_members, list) or not raw_members:
        errors.append("members must be a non-empty list of {agent: NAME}")
        raw_members = []
    seen = {orch_spec.name} if orch_spec else set()
    for i, entry in enumerate(raw_members, 1):
        where = f"members[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be a mapping with agent:")
            continue
        for key in sorted(set(entry) - MEMBER_KEYS):
            errors.append(f"{where}: unknown key {key!r}")
        spec = resolve(entry.get("agent"), where)
        if spec is None:
            continue
        if spec.name in seen:
            errors.append(f"{where}: {spec.name} appears twice in the team")
            continue
        seen.add(spec.name)
        members.append(Member(spec, entry.get("reports_to")
                              or (orch_spec.name if orch_spec else "")))
    by_name = {m.spec.name: m for m in members}
    for m in members:
        if m.reports_to and m.reports_to not in seen:
            errors.append(f"{m.spec.name}: reports_to {m.reports_to!r} is not in the team")

    review = doc.get("review") or {}
    if not isinstance(review, dict):
        errors.append("review must map worker: reviewer")
        review = {}
    for worker, reviewer in review.items():
        if worker not in by_name:
            errors.append(f"review: worker {worker!r} is not a member")
            continue
        if reviewer not in by_name:
            errors.append(f"review: reviewer {reviewer!r} for {worker} is not a member"
                          + (" (the orchestrator cannot verdict)"
                             if orch_spec and reviewer == orch_spec.name else ""))
            continue
        if worker == reviewer:
            errors.append(f"review: {worker} cannot review its own work")
            continue
        wcli, rcli = by_name[worker].spec.cli, by_name[reviewer].spec.cli
        if wcli == rcli:
            errors.append(f"review: {worker} and {reviewer} both run {wcli}; a reviewer "
                          f"sharing the worker's model shares its blind spots")
        by_name[worker].reviewer = reviewer
        by_name[reviewer].reviews.append(worker)
    for m in members:
        if m.spec.role == "worker" and not m.reviewer:
            warnings.append(f"{m.spec.name} is a worker with no reviewer in review:")

    kickoff = doc.get("kickoff") or DEFAULT_KICKOFF
    if not isinstance(kickoff, str):
        errors.append("kickoff must be a string")
        kickoff = DEFAULT_KICKOFF
    else:
        try:
            used = {f for _, f, _, _ in string.Formatter().parse(kickoff) if f}
            for bad in sorted(used - PLACEHOLDERS):
                errors.append(f"kickoff: unknown placeholder {{{bad}}} "
                              f"(known: {', '.join(sorted(PLACEHOLDERS))})")
        except ValueError as exc:
            errors.append(f"kickoff: {exc}")

    if errors:
        return None, errors, warnings
    return Team(path, name, project_dir, charter, epic, courier, idle, orch_spec,
                request.strip(), float(hours), members, dict(review), kickoff), errors, warnings


# ── spawn arguments, shared with `agentmux orchestrator start` and roll-call ────

def spawn_flags(spec, role=None):
    """The definition's flags for `agentmux spawn`, minus name, cwd and persona file.

    The same set boardteams.hire passes, so a pane is the same however it was started.
    AGENTMUX_NO_BYPASS=1 is the machine-wide brake, applied as hire applies it."""
    posture = spec.posture
    if os.environ.get("AGENTMUX_NO_BYPASS") == "1" and posture == "unrestricted":
        posture = "workspace-write"
    flags = ["--cli", spec.cli, "--agentdef", spec.name, "--posture", posture,
             "--role", role or spec.role]
    for flag, value in (("--model", spec.model), ("--auth", spec.auth),
                        ("--tools", ",".join(spec.tools)),
                        ("--deny-tools", ",".join(spec.tools_deny))):
        if value:
            flags += [flag, value]
    return flags


def spawn_argv(spec, cwd, role=None, agentmux=AGENTMUX):
    return [agentmux, "spawn", spec.name, "--cwd", str(cwd)] + spawn_flags(spec, role)


def cmd_spawn_args(args):
    """NUL-separated flags for one definition, persona written to --persona-out."""
    spec = agentdefs.resolve(args.name, args.project)
    if spec is None:
        _, problems = agentdefs.load_all(args.project)
        for p in problems:
            if Path(p["path"]).stem == args.name:
                print(f"teamfile: {p['path']}: {p['error']}", file=sys.stderr)
        print(f"teamfile: no usable definition {args.name!r} for {args.project}",
              file=sys.stderr)
        return 1
    flags = spawn_flags(spec, args.role)
    if args.persona_out:
        Path(args.persona_out).write_text(spec.persona + "\n", encoding="utf-8")
        flags += ["--persona-file", args.persona_out]
    sys.stdout.write("\0".join(flags) + "\0")
    return 0


# ── the live side ──────────────────────────────────────────────────────────────

def run(argv, env=None, capture=False, check=False):
    result = subprocess.run(argv, env=env, text=True,
                            capture_output=capture)
    if check and result.returncode:
        raise TeamError(f"failed ({result.returncode}): {shlex.join(argv)}"
                        + (f"\n{result.stderr.strip()}" if capture and result.stderr else ""))
    return result


def is_live(name):
    return subprocess.run(["tmux", "-L", SOCKET, "has-session", "-t", f"={name}"],
                          capture_output=True).returncode == 0


def team_env(team):
    env = os.environ.copy()
    env.setdefault("AGENTMUX_REPO", str(REPO))
    if team.idle_minutes is not None:
        env["AGENTMUX_IDLE_MINUTES"] = str(team.idle_minutes)
    return env


def kickoff_text(team, member):
    return team.kickoff.format(
        agent=member.spec.name, team=team.name, project=team.project,
        charter=team.charter or "(no charter)", epic=team.epic or "(no epic)",
        reports_to=member.reports_to, reviewed_by=member.reviewer or "(nobody)",
        reviews=", ".join(member.reviews) or "(nobody)")


def orchestrator_text(team):
    return ORCH_KICKOFF.format(
        agent=team.orchestrator.name, team=team.name,
        members=", ".join(f"{m.spec.name} ({m.spec.cli}, {m.spec.role})" for m in team.members),
        pairs=", ".join(f"{w} -> {r}" for w, r in team.review.items()) or "(none)",
        request=team.request)


def orchestrator_argv(team):
    return [AGENTMUX, "orchestrator", "start", "--agent", team.orchestrator.name,
            "--cwd", str(team.project), "--hours", f"{team.hours:g}",
            "--request", team.request]


def send(name, text, env):
    """Type the kickoff. `agentmux send` refuses when the pane shows a prompt (a trust
    dialog, an update notice) - that refusal is reported, never forced."""
    result = run([AGENTMUX, "send", name, text], env=env, capture=True)
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def settle(name, env, timeout=120):
    run([AGENTMUX, "wait", name, "--timeout", str(timeout), "--quiet", "4"], env=env,
        capture=True)


def cmd_up(team, dry_run):
    env = team_env(team)
    plan = []
    if team.courier:
        plan.append(("courier", [AGENTMUX, "courier", "start"], None))
    for m in team.members:
        plan.append((m.spec.name, spawn_argv(m.spec, team.project), m))
    plan.append((team.orchestrator.name, orchestrator_argv(team), None))

    if dry_run:
        if team.idle_minutes is not None:
            print(f"export AGENTMUX_IDLE_MINUTES={team.idle_minutes}")
        for label, argv, member in plan:
            extra = ["--persona-file", f"<{label}.persona>"] if member else []
            print(shlex.join(argv + extra))
        for m in team.members:
            print(f"# kickoff {m.spec.name}: {kickoff_text(team, m)}")
        print(f"# kickoff {team.orchestrator.name}: {orchestrator_text(team)}")
        return 0

    started = []
    for label, argv, member in plan:
        if member is None and label == "courier":
            run(argv, env=env)
            continue
        if is_live(label):
            print(f"{label}: already running - left alone")
            continue
        if member is None:                      # the orchestrator
            run(argv, env=env, check=True)
            started.append(label)
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".persona", encoding="utf-8") as fh:
            fh.write(member.spec.persona + "\n")
            fh.flush()
            run(argv + ["--persona-file", fh.name], env=env, check=True)
        started.append(label)

    attention = []
    for m in team.members:
        if m.spec.name not in started:
            continue
        settle(m.spec.name, env)
        ok, why = send(m.spec.name, kickoff_text(team, m), env)
        print(f"{m.spec.name}: kickoff {'sent' if ok else 'NOT sent - ' + why}")
        if not ok:
            attention.append(m.spec.name)
    orch = team.orchestrator.name
    if orch in started:
        settle(orch, env)
        if attention:
            print(f"{orch}: kickoff held back until {', '.join(attention)} "
                  f"{'is' if len(attention) == 1 else 'are'} briefed")
            attention.append(orch)
        else:
            ok, why = send(orch, orchestrator_text(team), env)
            print(f"{orch}: kickoff {'sent' if ok else 'NOT sent - ' + why}")
            if not ok:
                attention.append(orch)
    if attention:
        print("\nNeeds a person: these panes are showing a prompt (a folder-trust dialog "
              "on a first run in a new folder, an update notice).")
        for name in attention:
            print(f"  agentmux read {name}      # see it; answer with agentmux key {name} ...")
        print(f"then: agentmux teamfile kickoff {team.path} "
              + " ".join(f"--agent {n}" for n in attention))
        return 3
    print(f"\nteam {team.name} is up: {', '.join(team.agents())}")
    return 0


def cmd_kickoff(team, names):
    env = team_env(team)
    wanted = names or team.agents()
    rc = 0
    for m in team.members:
        if m.spec.name in wanted:
            ok, why = send(m.spec.name, kickoff_text(team, m), env)
            print(f"{m.spec.name}: kickoff {'sent' if ok else 'NOT sent - ' + why}")
            rc |= 0 if ok else 3
    if team.orchestrator.name in wanted:
        ok, why = send(team.orchestrator.name, orchestrator_text(team), env)
        print(f"{team.orchestrator.name}: kickoff {'sent' if ok else 'NOT sent - ' + why}")
        rc |= 0 if ok else 3
    return rc


def cmd_status(team):
    env = team_env(team)
    print(f"team {team.name}  project {team.project}  epic {team.epic or '-'}")
    rows = [(team.orchestrator, "orchestrator", "-")] + \
           [(m.spec, m.spec.role, m.reviewer or "-") for m in team.members]
    for spec, role, reviewer in rows:
        state = "up" if is_live(spec.name) else "down"
        print(f"  {spec.name:<20} {spec.cli:<7} {role:<12} {state:<5} reviewed by {reviewer}")
    run([AGENTMUX, "orchestrator", "status"], env=env)
    if team.courier:
        run([AGENTMUX, "courier", "status"], env=env)
    return 0


def cmd_down(team, dry_run):
    env = team_env(team)
    steps = [[AGENTMUX, "orchestrator", "stop"]]
    steps += [[AGENTMUX, "kill", m.spec.name] for m in team.members]
    for argv in steps:
        name = argv[-1] if argv[1] == "kill" else team.orchestrator.name
        if dry_run:
            print(shlex.join(argv))
            continue
        if argv[1] == "kill" and not is_live(name):
            print(f"{name}: not running")
            continue
        run(argv, env=env)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agentmux teamfile",
                                     description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "up", "status", "down", "kickoff"):
        p = sub.add_parser(name)
        p.add_argument("file")
        if name in ("up", "down"):
            p.add_argument("--dry-run", action="store_true")
        if name == "kickoff":
            p.add_argument("--agent", action="append", default=[])
    sa = sub.add_parser("spawn-args", help="NUL-separated spawn flags for one definition")
    sa.add_argument("name")
    sa.add_argument("--project", default=str(REPO))
    sa.add_argument("--role")
    sa.add_argument("--persona-out")
    args = parser.parse_args(argv)

    if args.command == "spawn-args":
        return cmd_spawn_args(args)

    team, errors, warnings = validate(args.file)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        print(f"teamfile: {args.file}: {len(errors)} error(s)", file=sys.stderr)
        return 2
    if args.command == "validate":
        print(f"teamfile: {args.file}: team {team.name} OK - orchestrator "
              f"{team.orchestrator.name} ({team.orchestrator.cli}), members "
              + ", ".join(f"{m.spec.name} ({m.spec.cli}, {m.spec.role})" for m in team.members))
        for w, r in team.review.items():
            print(f"  review: {w} ({team_member(team, w).spec.cli}) -> "
                  f"{r} ({team_member(team, r).spec.cli})")
        return 0
    try:
        if args.command == "up":
            return cmd_up(team, args.dry_run)
        if args.command == "status":
            return cmd_status(team)
        if args.command == "down":
            return cmd_down(team, args.dry_run)
        return cmd_kickoff(team, args.agent)
    except TeamError as exc:
        print(f"teamfile: {exc}", file=sys.stderr)
        return 1


def team_member(team, name):
    return next(m for m in team.members if m.spec.name == name)


if __name__ == "__main__":
    sys.exit(main())
