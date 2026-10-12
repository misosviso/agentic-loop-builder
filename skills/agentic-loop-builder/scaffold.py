#!/usr/bin/env python3
"""Turn a flow definition into a runnable loop skill.

    scaffold.py validate FLOW.json
    scaffold.py check    FLOW.json [--agents-dir DIR] [--no-env] [--json]
    scaffold.py schema   FLOW.json
    scaffold.py generate FLOW.json --out .claude/skills/<name> [--install-path PATH]
                         [--agent-path PATH | --no-agent] [--force]

`generate` writes, next to the skills directory, .claude/agents/<name>.md (the
orchestrator agent; kept if it exists, unless --force) and one
.claude/agents/<subagent>.md per custom subagent in the flow's "subagents"
(frontmatter always synced from flow.json, prompt body kept). Into --out:

    SKILL.md            the /<name> command, which forks into the orchestrator agent
                        (kept if it exists, unless --force)
    flow.json           copy of the flow definition
    state.schema.json   JSON Schema of this flow's state.json (always regenerated)
    state_manager.py    the generic engine (always refreshed)
    steps/*.md          one stub per step skill (never overwritten)

`check` is the readiness report to run before the first real run: flow
structure, whether each step's subagent can actually do the step (tools, MCP,
shell for required commands), the environment (preflight), every branch of the
flow simulated (rejections, automatic loops, waits that stay pending or fail),
and outputs that are only checked for existence.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from string import Template
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import state_manager as sm  # noqa: E402

TEMPLATES = HERE / "templates"


def validate(flow_path: Path, agents_dir: Optional[Path] = None) -> tuple[dict, list[str]]:
    """Load and validate a flow, including whether its subagents can do their
    steps. Raises EngineError on errors; returns the flow and warnings."""
    flow = sm.load_flow(flow_path)
    warnings = []
    for skill in step_skills(flow):
        if not (flow_path.parent / skill).exists():
            warnings.append(f"step skill {skill} does not exist yet (generate creates a stub)")
    if not flow.get("description"):
        warnings.append("flow has no 'description'; the command and agent need one")
    elif len(flow["description"]) > MAX_DESCRIPTION:
        warnings.append(f"flow description is {len(flow['description'])} characters; keep it under {MAX_DESCRIPTION}")
    defined = set(flow.get("subagents", {}))
    used = set(subagent_types(flow))
    for name in sorted(used - defined - BUILTIN_AGENTS):
        warnings.append(f"subagent_type '{name}' is not defined in 'subagents'; make sure .claude/agents/{name}.md exists")
    for name in sorted(defined - used):
        warnings.append(f"subagent '{name}' is defined but no step uses it")
    errors, cap_warnings = capability_report(flow, agent_dirs(flow_path, flow, agents_dir))
    warnings += cap_warnings
    if errors:
        raise sm.EngineError("some steps' subagents cannot do the step:\n- " + "\n- ".join(errors))
    return flow, warnings


MAX_DESCRIPTION = 200
BUILTIN_AGENTS = {"general-purpose", "Explore", "Plan"}


def iter_units(flow: dict):
    return sm.iter_units(flow)


def step_skills(flow: dict) -> list[str]:
    skills = []
    for _, udef in iter_units(flow):
        if udef.get("skill"):
            skills.append(udef["skill"])
        skills += [a["skill"] for a in udef.get("agents", []) if "skill" in a]
    return list(dict.fromkeys(skills))


# --------------------------------------------------------------------------- #
# Can each step's subagent actually do the step?
# --------------------------------------------------------------------------- #

class Toolset:
    """What an agent may use: allow is None for "every tool" (no `tools` list)."""

    def __init__(self, allow: Optional[list[str]], deny: list[str], source: str, read_only: bool = False):
        self.allow, self.deny, self.source, self.read_only = allow, deny, source, read_only

    @staticmethod
    def _match(entry: str, tool: str) -> bool:
        base = entry.split("(")[0].strip()
        if base.endswith("*"):
            return tool.startswith(base[:-1])
        return tool == base or (base.startswith("mcp__") and tool.startswith(base + "__"))

    def has(self, tool: str) -> bool:
        if any(self._match(e, tool) for e in self.deny):
            return False
        return self.allow is None or any(self._match(e, tool) for e in self.allow)


def _tool_list(value) -> list[str]:
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else value
    return [t.strip() for t in items if str(t).strip()]


def _read_frontmatter(path: Path) -> dict:
    """The few frontmatter fields the capability check needs (tools lists),
    from `key: a, b` lines or YAML `- item` lists."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---" not in text[4:]:
        return {}
    fields: dict = {}
    key = None
    for line in text[4:].split("\n---", 1)[0].splitlines():
        if re.match(r"^\s+-\s+", line) and key:
            fields.setdefault(key, []).append(line.split("-", 1)[1].strip().strip("'\""))
            continue
        m = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
        if m:
            key, value = m[1], m[2].strip()
            if value:
                fields[key] = value.strip("'\"")
    return fields


def agent_dirs(flow_path: Path, flow: dict, agents_dir: Optional[Path] = None) -> list[Path]:
    dirs = [Path(agents_dir)] if agents_dir else [default_agent_path(flow_path.parent, flow["name"]).parent]
    return dirs + [Path.home() / ".claude" / "agents"]


def resolve_toolset(name: str, flow: dict, dirs: list[Path]) -> Optional[Toolset]:
    if name == "general-purpose":
        return Toolset(None, [], "built-in")
    if name in ("Explore", "Plan"):
        return Toolset(None, ["Write", "Edit", "NotebookEdit"], "built-in, read-only", read_only=True)
    spec = flow.get("subagents", {}).get(name)
    if spec is not None:
        allow = _tool_list(spec.get("tools")) if "tools" in spec else None
        return Toolset(allow, _tool_list(spec.get("disallowedTools")), "defined in flow.json")
    for d in dirs:
        path = d / f"{name}.md"
        if path.is_file():
            fm = _read_frontmatter(path)
            allow = _tool_list(fm.get("tools")) if "tools" in fm else None
            return Toolset(allow, _tool_list(fm.get("disallowedTools")), str(path))
    return None


def _unit_workers(flow: dict, sdef: dict, udef: dict) -> list[tuple[str, str, bool, dict]]:
    """(label, subagent type, writes files?, requires) for each subagent a unit launches."""
    defaults = flow.get("defaults", {})
    base = udef.get("subagent_type", sdef.get("subagent_type", defaults.get("subagent_type", "general-purpose")))
    if sm.wait_kind(udef) == "command":
        return []  # the engine runs the check itself
    unit_req = udef.get("requires") or {}
    if udef.get("agents"):
        out = []
        for ad in udef["agents"]:
            req = {k: unit_req.get(k, []) + (ad.get("requires") or {}).get(k, []) for k in sm.REQUIRE_KEYS}
            out.append((f"'{udef['id']}' agent '{ad['id']}'", ad.get("subagent_type", base),
                        bool(sm.agent_artifacts(udef, ad)), req))
        return out
    writes = bool(sm.unit_artifacts(udef)) or bool(udef.get("produces_items")) or sm.wait_kind(udef) == "agent"
    return [(f"'{udef['id']}'", base, writes, unit_req)]


def capability_report(flow: dict, dirs: list[Path]) -> tuple[list[str], list[str]]:
    """Errors: a step's subagent lacks a tool the step needs. Warnings: the
    agent could not be found, or works around a missing tool."""
    errors, warnings = [], []
    for sdef, udef in iter_units(flow):
        for label, agent, writes, req in _unit_workers(flow, sdef, udef):
            ts = resolve_toolset(agent, flow, dirs)
            who = f"step {label} (subagent `{agent}`)"
            if ts is None:
                warnings.append(f"{who}: agent not found in {', '.join(map(str, dirs))}; its tools could not be checked")
                continue
            if writes and not (ts.has("Write") or ts.has("Edit")):
                if ts.has("Bash") and not ts.read_only:
                    warnings.append(f"{who} must write its output files but has no Write or Edit tool; it would have to use the shell")
                else:
                    errors.append(f"{who} must write its output files but has no Write, Edit or Bash tool ({ts.source})")
            if not (ts.has("Read") or ts.has("Bash")):
                errors.append(f"{who} cannot read its inputs: no Read or Bash tool ({ts.source})")
            for tool in req.get("tools", []):
                if not ts.has(tool):
                    errors.append(f"{who} needs the `{tool}` tool, which it does not have ({ts.source})")
            if req.get("commands") and not ts.has("Bash"):
                errors.append(f"{who} needs to run {', '.join(req['commands'])} but has no Bash tool ({ts.source})")
            for server in req.get("mcp", []):
                if not ts.has(f"mcp__{server}__any"):
                    errors.append(f"{who} needs the `{server}` MCP server's tools, which its tool list excludes ({ts.source}); "
                                  f"add `mcp__{server}` to its tools")
    return errors, warnings


def verification_gaps(flow: dict) -> list[str]:
    """Steps whose output is only checked for existence: the usual reason a
    generated flow is weaker than a hand-written one."""
    gaps = []
    for _, udef in iter_units(flow):
        if sm.wait_kind(udef) == "command":
            continue
        specs = sm.artifact_specs(udef)
        has_contract = any(set(s) - {"path", "description", "optional"} for s in specs)
        if specs and not has_contract and not udef.get("checks") and not udef.get("produces_items"):
            gaps.append(f"'{udef['id']}': {', '.join(s['path'] for s in specs)} only need to exist; add `sections`, "
                        "`json_schema`, `contains` or `checks` so a bad output is refused before anyone reviews it")
    return gaps


def explore_paths(flow_path: Path) -> list[dict]:
    """Simulate the happy path and each branch once: every gate rejected once,
    every repeat failing once and never passing, every `when` false, every wait
    pending and failing. A branch that never ends is an error."""
    flow = sm.load_flow(flow_path)
    scenarios: list[tuple[str, dict]] = [("happy path", {})]
    for _, udef in iter_units(flow):
        uid = udef["id"]
        gate = udef.get("gate")
        if gate:
            scenarios.append((f"gate {gate['id']} rejected once", {"rejects": {gate["id"]: 1}}))
            scenarios.append((f"gate {gate['id']} rejected every time", {"rejects": {gate["id"]: 1000}}))
        if udef.get("repeat"):
            rounds = udef["repeat"].get("max", sm.DEFAULT_MAX_ITERATIONS)
            scenarios.append((f"'{uid}' repeat condition fails once", {"until": {uid: 1}}))
            scenarios.append((f"'{uid}' repeat condition never passes", {"until": {uid: rounds * 10}}))
        if "when" in udef:
            scenarios.append((f"'{uid}' condition false (skipped)", {"skip": {uid}}))
        if udef.get("wait"):
            scenarios.append((f"wait '{uid}' pending twice", {"waits": {uid: 2}}))
            scenarios.append((f"wait '{uid}' reports failure once", {"wait_fails": {uid: 1}}))
            scenarios.append((f"wait '{uid}' fails every time", {"wait_fails": {uid: 1000}}))
    for sdef in flow["steps"]:
        if sdef.get("kind") == "foreach" and "when" in sdef:
            scenarios.append((f"'{sdef['id']}' condition false (skipped)", {"skip": {sdef["id"]}}))
    results = []
    for name, kwargs in scenarios:
        try:
            trace = sm.simulate(flow_path, **kwargs)
        except sm.EngineError as e:
            trace = [f"ERROR {e}"]
        end = trace[-1]
        if end.startswith("stopped after") or end.startswith("ERROR"):
            verdict = "error"
        elif name == "happy path" and end != "done":
            verdict = "error"
        else:
            verdict = "ok"
        results.append({"scenario": name, "end": end, "steps": len(trace), "verdict": verdict, "trace": trace})
    return results


def check(flow_path: Path, agents_dir: Optional[Path] = None, environment: bool = True) -> dict:
    """The readiness report: everything that would make a first run fail."""
    report: dict = {"ok": True, "errors": [], "warnings": [], "environment": None, "paths": [], "gaps": []}
    try:
        flow = sm.load_flow(flow_path)
    except sm.EngineError as e:
        report.update(ok=False, errors=[str(e)])
        return report
    try:
        _, report["warnings"] = validate(flow_path, agents_dir)
    except sm.EngineError as e:
        report["errors"].append(str(e))
    if environment:
        env = sm.preflight(flow)
        report["environment"] = env
        report["errors"] += [f"environment: {m}" for m in env["missing"]]
        report["warnings"] += [f"environment: {w}" for w in env["warnings"]]
    report["paths"] = explore_paths(flow_path)
    report["errors"] += [f"path '{p['scenario']}' ends with: {p['end']}" for p in report["paths"] if p["verdict"] == "error"]
    report["gaps"] = verification_gaps(flow)
    report["ok"] = not report["errors"]
    return report


def render_check(report: dict) -> str:
    lines = ["READY for a first run" if report["ok"] else "NOT READY", ""]
    for title, key in (("Errors", "errors"), ("Warnings", "warnings"), ("Outputs only checked for existence", "gaps")):
        if report[key]:
            lines += [f"{title}:"] + [f"  - {x}" for x in report[key]] + [""]
    if report["paths"]:
        lines.append("Paths (simulated):")
        for p in report["paths"]:
            mark = "ok " if p["verdict"] == "ok" else "ERR"
            lines.append(f"  [{mark}] {p['scenario']:<45} -> {p['end'][:110]}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# state.schema.json
# --------------------------------------------------------------------------- #

def state_schema(flow: dict) -> dict:
    gate = {
        "type": "object",
        "required": ["id", "when", "status", "decisions"],
        "properties": {
            "id": {"type": "string"},
            "when": {"enum": ["after", "before"]},
            "status": {"enum": ["pending", "approved", "rejected"]},
            "decisions": {"type": "array", "items": {"type": "object"}},
        },
    }
    agent = {
        "type": "object",
        "required": ["status", "artifacts"],
        "properties": {
            "status": {"enum": ["pending", "running", "done", "failed"]},
            "artifacts": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
            "started_at": {"type": ["string", "null"]},
            "completed_at": {"type": ["string", "null"]},
            "fix_failures": {"type": "integer", "minimum": 0},
        },
    }
    unit = {
        "type": "object",
        "required": ["status", "attempt", "artifacts", "feedback"],
        "properties": {
            "kind": {"const": "single"},
            "status": {"enum": list(sm.UNIT_STATUSES)},
            "attempt": {"type": "integer", "minimum": 0},
            "artifacts": {"type": "array", "items": {"type": "string"}},
            "previous_artifacts": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": ["string", "null"]},
            "feedback": {"type": ["string", "null"]},
            "feedback_files": {"type": "array", "items": {"type": "string"}},
            "feedback_history": {"type": "array", "items": {"type": "object"}},
            "loops": {"type": "integer", "minimum": 0, "description": "automatic loop-backs made by `repeat`"},
            "fix_failures": {"type": "integer", "minimum": 0, "description": "refused completions this attempt"},
            "wait": {
                "type": ["object", "null"],
                "properties": {
                    "started_at": {"type": "string"},
                    "next_check_at": {"type": "string"},
                    "polls": {"type": "integer"},
                    "last_status": {"enum": ["ready", "pending", "failed", None]},
                    "last_checked_at": {"type": ["string", "null"]},
                },
            },
            "error": {"type": ["string", "null"]},
            "started_at": {"type": ["string", "null"]},
            "completed_at": {"type": ["string", "null"]},
            "gate": {"$ref": "#/$defs/gate"},
            "agents": {
                "type": "object",
                "description": "present on steps that run parallel subagents, keyed by agent id",
                "additionalProperties": {"$ref": "#/$defs/agent"},
            },
        },
    }
    step_props = {}
    for sdef in flow["steps"]:
        if sdef.get("kind") != "foreach":
            what = f"skill {sdef['skill']}" if sdef.get("skill") else "waits on an external check"
            step_props[sdef["id"]] = {"$ref": "#/$defs/unit", "description": f"single step; {what}"}
            continue
        subs = [s["id"] for s in sdef["cycle"]]
        step_props[sdef["id"]] = {
            "description": f"foreach over the items of '{sdef['over']}'; cycle: {' -> '.join(subs)}",
            "type": "object",
            "required": ["kind", "status", "items"],
            "properties": {
                "kind": {"const": "foreach"},
                "status": {"enum": ["pending", "running", "done", "skipped"]},
                "items": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "required": ["id", "depends_on", "status", "item_file", "steps"],
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": ["string", "null"]},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                            "status": {"enum": ["pending", "running", "done", "failed"]},
                            "item_file": {"type": "string"},
                            "steps": {
                                "type": "object",
                                "required": subs,
                                "properties": {s: {"$ref": "#/$defs/unit"} for s in subs},
                                "additionalProperties": False,
                            },
                        },
                    },
                },
            },
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"{flow['name']} run state",
        "description": "Written only by state_manager.py. Generated by scaffold.py from flow.json; do not edit.",
        "type": "object",
        "required": ["version", "flow", "run_id", "inputs", "steps"],
        "properties": {
            "version": {"const": sm.STATE_VERSION},
            "flow": {"const": flow["name"]},
            "run_id": {"type": "string"},
            "inputs": {
                "type": "object",
                "required": list(flow.get("inputs", {})),
                "properties": {k: {"type": "string", "description": v} for k, v in flow.get("inputs", {}).items()},
            },
            "project_dir": {"type": "string", "description": "where checks and wait commands run"},
            "created_at": {"type": "string"},
            "updated_at": {"type": "string"},
            "steps": {
                "type": "object",
                "required": list(step_props),
                "properties": step_props,
            },
        },
        "$defs": {"unit": unit, "gate": gate, "agent": agent},
    }


# --------------------------------------------------------------------------- #
# SKILL.md and step stubs
# --------------------------------------------------------------------------- #

def _gate_cell(udef: dict) -> str:
    g = udef.get("gate")
    if not g:
        return ""
    text = f"**{g['id']}** {g.get('when', 'after')}"
    if g.get("on_reject"):
        text += f", reject → `{g['on_reject']}`"
    if g.get("guard"):
        text += ", hook-enforced"
    return text


def _subagent_cell(udef: dict, sdef: dict, flow: dict) -> str:
    defaults = flow.get("defaults", {})
    base = udef.get("subagent_type", sdef.get("subagent_type", defaults.get("subagent_type", "general-purpose")))
    if sm.wait_kind(udef) == "command":
        return "none (the engine runs the check)"
    if udef.get("agents"):
        return f"{len(udef['agents'])} in parallel: " + ", ".join(
            f"`{a['id']}` ({a.get('subagent_type', base)})" for a in udef["agents"])
    if not udef.get("run_in_subagent", sdef.get("run_in_subagent", defaults.get("run_in_subagent", True))):
        return "inline"
    return f"`{base}`"


def _control_cell(udef: dict) -> str:
    parts = []
    if "when" in udef:
        parts.append(f"only if {sm.describe_condition(udef['when'])}")
    rep = udef.get("repeat")
    if rep:
        parts.append(f"repeats until {sm.describe_condition(rep['until'])}, max {rep.get('max', sm.DEFAULT_MAX_ITERATIONS)} "
                     f"rounds → `{rep.get('back_to', udef['id'])}`; then {rep.get('on_exhausted', 'fail')}")
    wait = udef.get("wait")
    if wait:
        how = "a subagent check" if sm.wait_kind(udef) == "agent" else f"`{wait['check']['run']}`"
        on_failure = wait.get("on_failure", "fail")
        on_failure = f"→ `{on_failure['back_to']}`" if isinstance(on_failure, dict) else on_failure
        parts.append(f"waits on {how} every {wait.get('poll_every', '5m')}, up to {wait.get('timeout', 'no limit')}; "
                     f"on failure {on_failure}")
    checks = udef.get("checks", [])
    if checks:
        parts.append("checks: " + ", ".join(c["name"] if isinstance(c, dict) and c.get("name") else f"`{sm.check_command(c)}`"
                                           for c in checks))
    return "; ".join(parts)


def _artifacts_cell(udef: dict) -> str:
    cells = []
    for spec in sm.artifact_specs(udef):
        verified = set(spec) - {"path", "description", "optional"}
        cells.append(spec["path"] + (" ✓" if verified else ""))
    return ", ".join(cells)


def steps_table(flow: dict, link_prefix: str = "") -> str:
    rows = ["| # | Step | Gate | Artifacts (✓ = contract) | Subagents | Runs when / loops / waits |",
            "|---|---|---|---|---|---|"]
    for n, sdef in enumerate(flow["steps"], 1):
        if sdef.get("kind") == "foreach":
            when = f"only if {sm.describe_condition(sdef['when'])}" if "when" in sdef else ""
            rows.append(f"| {n} | `{sdef['id']}`: for each item of `{sdef['over']}` "
                        f"(max {sdef.get('max_iterations', flow.get('defaults', {}).get('max_iterations', sm.DEFAULT_MAX_ITERATIONS))} rounds) | | | | {when} |")
            units = [(f"| | ↳ `{sub['id']}`", sub) for sub in sdef["cycle"]]
        else:
            units = [(f"| {n} | `{sdef['id']}`", sdef)]
        for head, udef in units:
            link = f" ([{udef['skill']}]({link_prefix}{udef['skill']}))" if udef.get("skill") else ""
            rows.append(f"{head}{link} | {_gate_cell(udef)} | {_artifacts_cell(udef)} | "
                        f"{_subagent_cell(udef, sdef, flow)} | {_control_cell(udef)} |")
    return "\n".join(rows)


def hook_settings(flow: dict, install_path: str) -> dict:
    tools = {(u.get("gate") or {}).get("guard", {}).get("tool") for _, u in iter_units(flow) if (u.get("gate") or {}).get("guard")}
    matcher = "*" if None in tools else "|".join(sorted(tools))
    cmd = (f'python3 "$CLAUDE_PROJECT_DIR"/{install_path}/state_manager.py '
           f'--runs-dir "$CLAUDE_PROJECT_DIR"/{sm.DEFAULT_RUNS_DIR} guard')
    return {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [{"type": "command", "command": cmd}]}]}}


def hard_gates_section(flow: dict, install_path: str) -> str:
    guarded = [(u["id"], u["gate"]) for _, u in iter_units(flow) if (u.get("gate") or {}).get("guard")]
    if not guarded:
        return ""
    lines = [
        "",
        "## Hard gates",
        "",
        "These gates are also enforced by a `PreToolUse` hook. The matching tool call is blocked unless the gated step is the run's current step:",
        "",
    ]
    for uid, g in guarded:
        tool = g["guard"].get("tool", "any tool")
        lines.append(f"- **{g['id']}** before `{uid}`: {tool} calls matching `{g['guard']['pattern']}`")
    lines += [
        "",
        "If the hook blocks you, the gate hasn't been approved. Go back to the loop and don't work around it.",
        "The hook is installed with this snippet in `.claude/settings.json`:",
        "",
        "```json",
        json.dumps(hook_settings(flow, install_path), indent=2),
        "```",
    ]
    return "\n".join(lines)


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid YAML scalar


def _command(flow: dict) -> str:
    return f"/{flow['name']}"


def orchestrator_body(flow: dict, install_path: str, as_agent: bool) -> str:
    name = flow["name"]
    cmd = _command(flow)
    inputs = flow.get("inputs", {})
    if as_agent:
        role = f"""
## Your role

You are the orchestrator agent of this flow, and an orchestrator **only**:

- You have no file-editing tools.
- Your shell is for the engine command. Use Read, Glob and Grep only to look at artifacts when presenting a gate.
- Every step, including any marked `run_in_subagent: false`, is done by subagents you launch.

You are started in one of two ways.

### Command mode

You are in command mode when your task says you were started by `{cmd}`. You run as a forked subagent and **cannot talk to the user**. Your task carries the arguments:

```
<RUN_ID>                                 start the run, or resume it
<RUN_ID> approve <GATE> [note]           the user approved the waiting gate
<RUN_ID> reject <GATE> <feedback>        the user rejected it
<RUN_ID> retry <STEP>                    the user wants a failed step retried
(nothing)                                list the runs
```

1. If a decision was given, apply it first with the engine (`approve`, `reject` or `retry`). Its words are the user's: pass them on unchanged as `--note` or `--feedback`.
2. Then run the loop below until the engine returns `await_gate`, `failed` or `done`, or `wait` with the next check more than ten minutes away. These are the points where the procedure says to ask or tell the user.
3. Stop there, and return a short report as your final answer. It should give:
   - the run and where it stands;
   - for a gate: its id, its prompt, the step summary, and the files to review, with one line on each;
   - the exact commands the user can type next, e.g. `{cmd} <RUN_ID> approve <GATE> [note]` and `{cmd} <RUN_ID> reject <GATE> <what must change>`, or `{cmd} <RUN_ID> retry <STEP>`;
   - when waiting: what for, the last result, when the next check is due, and that `{cmd} <RUN_ID>` resumes it.

Never approve or reject a gate that the arguments didn't decide.

### Session mode

You are in session mode when you run as the main session (`claude --agent {name}`). You talk to the user directly: follow the procedure as written, asking at every gate.
"""
        inline = ""
    else:
        role = (f"\nThis flow also ships as an orchestrator agent, so you can run it in its own session with "
                f"`claude --agent {name}`.\n")
        inline = " If `run_in_subagent` is false, do the step yourself by following the `skill` file."
    return Template((TEMPLATES / "orchestrator.md.tmpl").read_text()).substitute(
        name=name,
        title=name.replace("-", " ").title(),
        role=role,
        inline_rule=inline,
        engine=f"{install_path}/state_manager.py",
        runs_dir=sm.DEFAULT_RUNS_DIR,
        steps_table=steps_table(flow, f"../skills/{name}/" if as_agent and install_path == f".claude/skills/{name}" else ""),
        inputs_flags="".join(f" --input {k}=<{v}>" for k, v in inputs.items()),
        hard_gates=hard_gates_section(flow, install_path),
    )


def _description(flow: dict) -> str:
    return " ".join((flow.get("description") or f"Run the {flow['name']} flow.").split())


def skill_md(flow: dict, install_path: str, with_agent: bool = True) -> str:
    """The /<flow> command. With an agent, it forks into the orchestrator agent,
    which advances the run to the next gate; otherwise it orchestrates inline."""
    name = flow["name"]
    if not with_agent:
        front = ["---", f"name: {name}", f"description: {_yaml(_description(flow))}",
                 f"argument-hint: {_yaml('<run-id>')}", "---", ""]
        return "\n".join(front) + orchestrator_body(flow, install_path, as_agent=False)
    cmd = _command(flow)
    front = [
        "---",
        f"name: {name}",
        f"description: {_yaml(_description(flow))}",
        f"argument-hint: {_yaml('<run-id> [approve <gate> [note] | reject <gate> <feedback> | retry <step>]')}",
        "context: fork",
        f"agent: {name}",
        "background: false",
        "disable-model-invocation: true",
        "---",
        "",
    ]
    body = f"""You were started by the `{cmd}` command, so you are in **command mode**.

Arguments: $ARGUMENTS

Advance the `{name}` run named in the arguments, as your instructions describe:

1. Apply the user's decision first, if the arguments carry one.
2. Run the loop until the next gate, failure or the end of the flow.
3. Return the report, with the exact `{cmd} …` commands the user can type next.
"""
    return "\n".join(front) + body


def subagent_types(flow: dict) -> list[str]:
    defaults = flow.get("defaults", {})
    types = []
    for sdef, udef in iter_units(flow):
        if sm.wait_kind(udef) == "command":
            continue
        base = udef.get("subagent_type", sdef.get("subagent_type", defaults.get("subagent_type", "general-purpose")))
        types.append(base)
        types += [a.get("subagent_type", base) for a in udef.get("agents", [])]
    return list(dict.fromkeys(types))


def agent_md(flow: dict, install_path: str) -> str:
    name = flow["name"]
    orch = flow.get("orchestrator", {})
    description = (f"Orchestrator of the {name} flow: runs each step in subagents and stops at gates. "
                   f"Started by {_command(flow)}; do not delegate to it.")
    tools = [f"Agent({', '.join(subagent_types(flow))})", "Bash", "Read", "Glob", "Grep", "AskUserQuestion"]
    front = ["---", f"name: {name}", f"description: {_yaml(description)}", f"tools: {', '.join(tools)}"]
    front += [f"{key}: {orch[key]}" for key in ("model", "color", "effort") if orch.get(key)]
    initial = (f"Session mode: start or resume a run of the {name} flow. The run id is whatever follows this sentence; "
               "if nothing follows, list the existing runs and ask me which one to resume or what new run id to start.")
    front += [f"initialPrompt: {_yaml(initial)}", "---", ""]
    return "\n".join(front) + orchestrator_body(flow, install_path, as_agent=True)


def _frontmatter_value(value) -> str:
    if isinstance(value, list):
        return ", ".join(value) if all(isinstance(v, str) for v in value) else json.dumps(value)
    return _yaml(value) if isinstance(value, str) and (":" in value or value != value.strip() or "#" in value) else str(value)


def subagent_frontmatter(name: str, spec: dict) -> str:
    lines = ["---", f"name: {name}", f"description: {_yaml(' '.join(spec['description'].split()))}"]
    for key in sm.SUBAGENT_FIELDS:
        if key != "description" and key in spec:
            lines.append(f"{key}: {_frontmatter_value(spec[key])}")
    return "\n".join(lines + ["---", ""])


def subagent_body(flow: dict, name: str) -> str:
    steps = [u["id"] for sdef, u in iter_units(flow)
             if u.get("subagent_type", sdef.get("subagent_type", flow.get("defaults", {}).get("subagent_type"))) == name]
    steps += [f"{u['id']} ({a['id']})" for _, u in iter_units(flow) for a in u.get("agents", []) if a.get("subagent_type") == name]
    used = ", ".join(f"`{s}`" for s in dict.fromkeys(steps)) or "none yet"
    return f"""
You are `{name}`, a specialist subagent of the `{flow['name']}` flow. The flow's orchestrator launches you for these steps: {used}.

## Role

TODO: what you are an expert at, the conventions and quality bar you hold, and the tools you rely on.

## How you work

- Your task names a step instructions file. Read it first and follow it; it defines your inputs, outputs and done-criteria.
- Write only to the output paths your task gives you. Never read or modify the run's `state.json` or call the state engine.
- Finish with a reply of at most 5 lines: what you did, what you wrote, and anything the reviewer should look at.
"""


def write_subagent(path: Path, flow: dict, name: str) -> str:
    """Write a custom subagent: frontmatter always follows flow.json, the body
    (the agent's prompt) is kept once it exists."""
    front = subagent_frontmatter(name, flow["subagents"][name])
    if path.exists():
        text = path.read_text(encoding="utf-8")
        has_front = text.startswith("---\n") and "\n---\n" in text
        new = front + (text.split("\n---\n", 1)[1] if has_front else text)
        if new == text:
            return "kept"
        path.write_text(new, encoding="utf-8")
        return "synced"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(front + subagent_body(flow, name), encoding="utf-8")
    return "written"


def step_stub(flow: dict, sdef: dict, udef: dict, agent: Optional[dict] = None) -> str:
    refs = list(sdef.get("inputs", sdef.get("needs", [])))
    if sdef.get("kind") == "foreach" and sdef["over"] not in refs:
        refs.append(sdef["over"])
    steps = {s["id"]: s for s in flow["steps"]}
    inputs = [f"- Run inputs: {', '.join(flow.get('inputs', {})) or 'none'}"]
    for ref in refs:
        arts = sm.unit_artifacts(steps[ref]) or [f"artifacts of `{ref}`"]
        inputs.append(f"- From `{ref}`: {', '.join(arts)}")
    if sdef.get("kind") == "foreach":
        inputs.append("- The work item (one entry of the items file), as JSON")
        idx = sdef["cycle"].index(udef)
        for prev in sdef["cycle"][:idx]:
            inputs.append(f"- From `{prev['id']}` for the same item: {', '.join(sm.unit_artifacts(prev)) or 'nothing'}")
    if not refs and sdef.get("kind") != "foreach":
        inputs.append("- No earlier artifacts: this is the first step.")
    if agent:
        names = sm.agent_artifacts(udef, agent)
    elif udef.get("agents") and "artifacts" in udef:
        names = [sm.norm_artifact(a)["path"] for a in udef["artifacts"]]  # still templated with {agent}
    else:
        names = sm.unit_artifacts(udef)
    outputs = [f"- `{a}`: TODO describe its content and structure" for a in names] or [
        "- No files. Report the result in your reply."]
    if sm.wait_kind(udef) == "agent":
        status = sm.wait_status_path(udef, sdef.get("kind") == "foreach")
        outputs.append(f"- `{status}`: `{{\"status\": \"ready\" | \"pending\" | \"failed\", \"summary\": \"...\"}}`. "
                       "This step **checks something external**: report `pending` if it is not ready yet (you will be "
                       "asked again later), `failed` only if it definitely failed.")
    checked = sm.describe_contract(udef, agent)
    if udef.get("agents"):
        users = [a for a in udef["agents"] if (a.get("skill", udef["skill"]) == (agent or {}).get("skill", udef["skill"]))]
        outputs.append(
            f"\nThis step runs as {len(udef['agents'])} subagents **in parallel**. "
            + ("These instructions are used by: " + ", ".join(f"`{a['id']}`" for a in users) + ". " if users else "")
            + "Each subagent gets its own `focus` and output paths (`{agent}` is replaced by its id). "
              "Stay in your lane and write only your own files.")
    pi = udef.get("produces_items")
    if pi:
        schema = sm.flow_schemas(flow).get(pi.get("item_schema"))
        where = f"a JSON object with a `{pi['key']}` list" if pi.get("key") else "a JSON list"
        outputs.append(f"\n`{pi['file']}` must be {where}. The engine validates every item"
                       + (" against this schema:\n\n```json\n" + json.dumps(schema, indent=2) + "\n```" if schema else "."))
    gate = udef.get("gate")
    if gate and gate.get("when", "after") == "after":
        outputs.append(f"\nThe user reviews these at gate **{gate['id']}**, so make them easy to review.")
    limit = udef.get("max_fix_attempts", flow.get("defaults", {}).get("max_fix_attempts", sm.DEFAULT_MAX_FIX_ATTEMPTS))
    checked_text = "\n".join(f"- {c}" for c in checked)
    checked_text += f"\n\nThe engine refuses output that fails these, and the step fails after {limit} refusals."
    if any(gap.startswith(f"'{udef['id']}'") for gap in verification_gaps({"steps": [udef]})):
        checked_text += ("\n\nTODO: only that the output files exist. Add `sections`, `contains`, `json_schema` or "
                         "`checks` to this step in flow.json so bad output is refused before anyone reviews it.")
    return Template((TEMPLATES / "step.md.tmpl").read_text()).substitute(
        id=f"{udef['id']} ({agent['id']})" if agent else udef["id"], flow=flow["name"], inputs="\n".join(inputs),
        outputs="\n".join(outputs), checked=checked_text)


# --------------------------------------------------------------------------- #
# generate
# --------------------------------------------------------------------------- #

def _rel(path: Path) -> str:
    return os.path.relpath(path) if Path(path).resolve().is_relative_to(Path.cwd()) else str(path)


def default_agent_path(out: Path, name: str) -> Path:
    """.claude/skills/<name> -> .claude/agents/<name>.md"""
    if out.resolve().parent.name == "skills":
        return out.resolve().parent.parent / "agents" / f"{name}.md"
    return out / "agents" / f"{name}.md"


def generate(flow_path: Path, out: Path, install_path: str = "", force: bool = False,
             agent_path: Optional[Path] = None, with_agent: bool = True) -> dict:
    flow, warnings = validate(flow_path, Path(agent_path).parent if agent_path else default_agent_path(out, sm.load_flow(flow_path)["name"]).parent)
    out.mkdir(parents=True, exist_ok=True)
    install_path = (install_path or f".claude/skills/{flow['name']}").rstrip("/")
    written, kept = [], []

    def write(rel: str, text: str, overwrite: bool) -> None:
        path = out / rel
        if path.exists() and not overwrite:
            kept.append(rel)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(rel)

    if flow_path.resolve() != (out / "flow.json").resolve():
        shutil.copyfile(flow_path, out / "flow.json")
        written.append("flow.json")
    shutil.copyfile(HERE / "state_manager.py", out / "state_manager.py")
    written.append("state_manager.py")
    write("state.schema.json", json.dumps(state_schema(flow), indent=2) + "\n", overwrite=True)
    write("SKILL.md", skill_md(flow, install_path, with_agent), overwrite=force)
    for sdef, udef in iter_units(flow):
        if udef.get("skill"):
            write(udef["skill"], step_stub(flow, sdef, udef), overwrite=False)
        for ad in udef.get("agents", []):
            if ad.get("skill") and ad["skill"] != udef["skill"]:
                write(ad["skill"], step_stub(flow, sdef, udef, ad), overwrite=False)
    agent_file = None
    if with_agent:
        agent_file = Path(agent_path) if agent_path else default_agent_path(out, flow["name"])
        if agent_file.exists() and not force:
            kept.append(_rel(agent_file))
        else:
            agent_file.parent.mkdir(parents=True, exist_ok=True)
            agent_file.write_text(agent_md(flow, install_path), encoding="utf-8")
            written.append(_rel(agent_file))
    agents_dir = (agent_file or default_agent_path(out, flow["name"])).parent
    subagents = {}
    for name in flow.get("subagents", {}):
        path = agents_dir / f"{name}.md"
        outcome = write_subagent(path, flow, name)
        subagents[name] = _rel(path)
        (kept if outcome == "kept" else written).append(_rel(path))
    result = {"ok": True, "out": str(out), "agent": _rel(agent_file) if agent_file else None,
              "subagents": subagents, "written": written, "kept": kept,
              "warnings": [w for w in warnings if "generate creates a stub" not in w]}
    if any((u.get("gate") or {}).get("guard") for _, u in iter_units(flow)):
        result["hook_settings"] = hook_settings(flow, install_path)
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="scaffold.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="check a flow definition and its subagents' tools")
    v.add_argument("flow")
    v.add_argument("--agents-dir", help="where existing agents live (default: .claude/agents next to the skills dir)")
    c = sub.add_parser("check", help="readiness report before a first run")
    c.add_argument("flow")
    c.add_argument("--agents-dir", help="where existing agents live (default: .claude/agents next to the skills dir)")
    c.add_argument("--no-env", action="store_true", help="skip the environment preflight (e.g. when checking on another machine)")
    c.add_argument("--json", action="store_true", help="print the report as JSON")
    sub.add_parser("schema", help="print the state JSON Schema for a flow").add_argument("flow")
    g = sub.add_parser("generate", help="write the loop skill")
    g.add_argument("flow")
    g.add_argument("--out", required=True, help="skill directory to write, e.g. .claude/skills/development-loop")
    g.add_argument("--install-path", default="",
                   help="where the skill lives relative to the project root (default: .claude/skills/<name>)")
    g.add_argument("--agent-path", help="orchestrator agent file (default: .claude/agents/<name>.md next to the skills dir)")
    g.add_argument("--no-agent", action="store_true", help="don't generate the orchestrator agent")
    g.add_argument("--force", action="store_true", help="also regenerate SKILL.md and the agent file")
    args = p.parse_args(argv)
    try:
        if args.cmd == "validate":
            _, warnings = validate(Path(args.flow), Path(args.agents_dir) if args.agents_dir else None)
            print(json.dumps({"ok": True, "warnings": warnings}, indent=2))
        elif args.cmd == "check":
            report = check(Path(args.flow), Path(args.agents_dir) if args.agents_dir else None, not args.no_env)
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print(render_check(report))
            return 0 if report["ok"] else 1
        elif args.cmd == "schema":
            print(json.dumps(state_schema(sm.load_flow(Path(args.flow))), indent=2))
        else:
            print(json.dumps(generate(Path(args.flow), Path(args.out), args.install_path, args.force,
                                      Path(args.agent_path) if args.agent_path else None, not args.no_agent), indent=2))
        return 0
    except sm.EngineError as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
