#!/usr/bin/env python3
"""Turn a flow definition into a runnable loop skill.

    scaffold.py validate FLOW.json
    scaffold.py schema   FLOW.json
    scaffold.py generate FLOW.json --out .claude/skills/<name> [--install-path PATH]
                         [--agent-path PATH | --no-agent] [--force]

`generate` writes the orchestrator agent to .claude/agents/<name>.md (kept if it
exists, unless --force) and into --out:

    SKILL.md            orchestrator (kept if it exists, unless --force)
    flow.json           copy of the flow definition
    state.schema.json   JSON Schema of this flow's state.json (always regenerated)
    state_manager.py    the generic engine (always refreshed)
    steps/*.md          one stub per step skill (never overwritten)

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from string import Template
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import state_manager as sm  # noqa: E402

TEMPLATES = HERE / "templates"


def validate(flow_path: Path) -> tuple[dict, list[str]]:
    """Load and validate a flow. Returns the flow and non-fatal warnings."""
    flow = sm.load_flow(flow_path)
    warnings = []
    for _, udef in iter_units(flow):
        for skill in dict.fromkeys([udef["skill"]] + [a["skill"] for a in udef.get("agents", []) if "skill" in a]):
            if not (flow_path.parent / skill).exists():
                warnings.append(f"step skill {skill} does not exist yet (generate creates a stub)")
    if not flow.get("description"):
        warnings.append("flow has no 'description'; the orchestrator skill needs one to be discovered")
    return flow, warnings


def iter_units(flow: dict):
    for sdef in flow["steps"]:
        for udef in sdef.get("cycle", [sdef]):
            yield sdef, udef


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
        },
    }
    unit = {
        "type": "object",
        "required": ["status", "attempt", "artifacts", "feedback"],
        "properties": {
            "kind": {"const": "single"},
            "status": {"enum": ["pending", "running", "awaiting_gate", "done", "failed"]},
            "attempt": {"type": "integer", "minimum": 0},
            "artifacts": {"type": "array", "items": {"type": "string"}},
            "previous_artifacts": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": ["string", "null"]},
            "feedback": {"type": ["string", "null"]},
            "feedback_history": {"type": "array", "items": {"type": "object"}},
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
            step_props[sdef["id"]] = {"$ref": "#/$defs/unit", "description": f"single step; skill {sdef['skill']}"}
            continue
        subs = [s["id"] for s in sdef["cycle"]]
        step_props[sdef["id"]] = {
            "description": f"foreach over the items of '{sdef['over']}'; cycle: {' -> '.join(subs)}",
            "type": "object",
            "required": ["kind", "status", "items"],
            "properties": {
                "kind": {"const": "foreach"},
                "status": {"enum": ["pending", "running", "done"]},
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
    if udef.get("agents"):
        return f"{len(udef['agents'])} in parallel: " + ", ".join(f"`{a['id']}`" for a in udef["agents"])
    default = flow.get("defaults", {}).get("run_in_subagent", True)
    return "yes" if udef.get("run_in_subagent", sdef.get("run_in_subagent", default)) else "no"


def steps_table(flow: dict, link_prefix: str = "") -> str:
    rows = ["| # | Step | Gate | Artifacts | Subagents |", "|---|---|---|---|---|"]
    for n, sdef in enumerate(flow["steps"], 1):
        if sdef.get("kind") == "foreach":
            rows.append(f"| {n} | `{sdef['id']}`: for each item of `{sdef['over']}` "
                        f"(max {sdef.get('max_iterations', flow.get('defaults', {}).get('max_iterations', sm.DEFAULT_MAX_ITERATIONS))} rounds) | | | |")
            units = [(f"| | ↳ `{sub['id']}`", sub) for sub in sdef["cycle"]]
        else:
            units = [(f"| {n} | `{sdef['id']}`", sdef)]
        for head, udef in units:
            rows.append(f"{head} ([{udef['skill']}]({link_prefix}{udef['skill']})) | {_gate_cell(udef)} | "
                        f"{', '.join(sm.unit_artifacts(udef))} | {_subagent_cell(udef, sdef, flow)} |")
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


def orchestrator_body(flow: dict, install_path: str, as_agent: bool) -> str:
    name = flow["name"]
    inputs = flow.get("inputs", {})
    if as_agent:
        role = (
            "\n## Your role\n\n"
            "You are the dedicated orchestrator agent for this flow, running as the main session "
            f"(`claude --agent {name}`). You are an orchestrator **only**:\n\n"
            "- You have no file-editing tools.\n"
            "- Your shell is for the engine command. Use Read, Glob and Grep only to look at artifacts when presenting a gate.\n"
            "- Every step, including any marked `run_in_subagent: false`, is done by subagents you launch.\n"
        )
        inline = ""
    else:
        role = (f"\nThis flow also ships as a dedicated orchestrator agent. To run it as its own session, "
                f"use `claude --agent {name}`.\n")
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


def skill_md(flow: dict, install_path: str) -> str:
    inputs = flow.get("inputs", {})
    description = (flow.get("description") or f"Run the {flow['name']} flow.").replace("\n", " ")
    hint = "<run-id>" + ("".join(f" [{k}]" for k in list(inputs)[1:]) if len(inputs) > 1 else "")
    front = ["---", f"name: {flow['name']}", f"description: {_yaml(description)}", f"argument-hint: {_yaml(hint)}", "---", ""]
    return "\n".join(front) + orchestrator_body(flow, install_path, as_agent=False)


def subagent_types(flow: dict) -> list[str]:
    defaults = flow.get("defaults", {})
    types = []
    for sdef, udef in iter_units(flow):
        base = udef.get("subagent_type", sdef.get("subagent_type", defaults.get("subagent_type", "general-purpose")))
        types.append(base)
        types += [a.get("subagent_type", base) for a in udef.get("agents", [])]
    return list(dict.fromkeys(types))


def agent_md(flow: dict, install_path: str) -> str:
    name = flow["name"]
    orch = flow.get("orchestrator", {})
    what = (flow.get("description") or "").split(". Use when")[0].rstrip(".")
    what = what[:1].lower() + what[1:]
    description = (
        f"Orchestrator for the {name} flow{': ' + what if what else ''}. "
        "It only coordinates: it drives the state engine, launches a subagent per step (several in parallel where the flow says so) "
        f"and stops at every gate for the user's decision. Start it as the main session with `claude --agent {name}`; "
        "it needs the user at its gates, so do not delegate to it as a subagent."
    )
    tools = [f"Agent({', '.join(subagent_types(flow))})", "Bash", "Read", "Glob", "Grep", "AskUserQuestion"]
    front = ["---", f"name: {name}", f"description: {_yaml(description)}", f"tools: {', '.join(tools)}"]
    front += [f"{key}: {orch[key]}" for key in ("model", "color", "effort") if orch.get(key)]
    initial = (f"Start or resume a run of the {name} flow. The run id is whatever follows this sentence; "
               "if nothing follows, list the existing runs and ask me which one to resume or what new run id to start.")
    front += [f"initialPrompt: {_yaml(initial)}", "---", ""]
    return "\n".join(front) + orchestrator_body(flow, install_path, as_agent=True)


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
        names = udef["artifacts"]  # still templated with {agent}
    else:
        names = sm.unit_artifacts(udef)
    outputs = [f"- `{a}`: TODO describe its content and structure" for a in names] or [
        "- No files. Report the result in your reply."]
    if udef.get("agents"):
        users = [a for a in udef["agents"] if (a.get("skill", udef["skill"]) == (agent or {}).get("skill", udef["skill"]))]
        outputs.append(
            f"\nThis step runs as {len(udef['agents'])} subagents **in parallel**. "
            + ("These instructions are used by: " + ", ".join(f"`{a['id']}`" for a in users) + ". " if users else "")
            + "Each subagent gets its own `focus` and output paths (`{agent}` is replaced by its id). "
              "Stay in your lane and write only your own files.")
    pi = udef.get("produces_items")
    if pi:
        schema = flow.get("item_schemas", {}).get(pi.get("item_schema"))
        where = f"a JSON object with a `{pi['key']}` list" if pi.get("key") else "a JSON list"
        outputs.append(f"\n`{pi['file']}` must be {where}. The engine validates every item"
                       + (" against this schema:\n\n```json\n" + json.dumps(schema, indent=2) + "\n```" if schema else "."))
    gate = udef.get("gate")
    if gate and gate.get("when", "after") == "after":
        outputs.append(f"\nThe user reviews these at gate **{gate['id']}**, so make them easy to review.")
    return Template((TEMPLATES / "step.md.tmpl").read_text()).substitute(
        id=f"{udef['id']} ({agent['id']})" if agent else udef["id"], flow=flow["name"], inputs="\n".join(inputs), outputs="\n".join(outputs))


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
    flow, warnings = validate(flow_path)
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
    write("SKILL.md", skill_md(flow, install_path), overwrite=force)
    for sdef, udef in iter_units(flow):
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
    result = {"ok": True, "out": str(out), "agent": _rel(agent_file) if agent_file else None,
              "written": written, "kept": kept,
              "warnings": [w for w in warnings if "generate creates a stub" not in w]}
    if any((u.get("gate") or {}).get("guard") for _, u in iter_units(flow)):
        result["hook_settings"] = hook_settings(flow, install_path)
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="scaffold.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="check a flow definition").add_argument("flow")
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
            _, warnings = validate(Path(args.flow))
            print(json.dumps({"ok": True, "warnings": warnings}, indent=2))
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
