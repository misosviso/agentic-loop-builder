#!/usr/bin/env python3
"""Turn a flow definition into a runnable loop skill.

    scaffold.py validate FLOW.json
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
        warnings.append("flow has no 'description'; the command and agent need one")
    elif len(flow["description"]) > MAX_DESCRIPTION:
        warnings.append(f"flow description is {len(flow['description'])} characters; keep it under {MAX_DESCRIPTION}")
    defined = set(flow.get("subagents", {}))
    used = set(subagent_types(flow))
    for name in sorted(used - defined - BUILTIN_AGENTS):
        warnings.append(f"subagent_type '{name}' is not defined in 'subagents'; make sure .claude/agents/{name}.md exists")
    for name in sorted(defined - used):
        warnings.append(f"subagent '{name}' is defined but no step uses it")
    return flow, warnings


MAX_DESCRIPTION = 200
BUILTIN_AGENTS = {"general-purpose", "Explore", "Plan"}


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
    defaults = flow.get("defaults", {})
    base = udef.get("subagent_type", sdef.get("subagent_type", defaults.get("subagent_type", "general-purpose")))
    if udef.get("agents"):
        return f"{len(udef['agents'])} in parallel: " + ", ".join(
            f"`{a['id']}` ({a.get('subagent_type', base)})" for a in udef["agents"])
    if not udef.get("run_in_subagent", sdef.get("run_in_subagent", defaults.get("run_in_subagent", True))):
        return "inline"
    return f"`{base}`"


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
2. Then run the loop below until the engine returns `await_gate`, `failed` or `done`. These are the points where the procedure says to ask or tell the user.
3. Stop there, and return a short report as your final answer. It should give:
   - the run and where it stands;
   - for a gate: its id, its prompt, the step summary, and the files to review, with one line on each;
   - the exact commands the user can type next, e.g. `{cmd} <RUN_ID> approve <GATE> [note]` and `{cmd} <RUN_ID> reject <GATE> <what must change>`, or `{cmd} <RUN_ID> retry <STEP>`.

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
    write("SKILL.md", skill_md(flow, install_path, with_agent), overwrite=force)
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
