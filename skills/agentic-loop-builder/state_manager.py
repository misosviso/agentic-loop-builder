#!/usr/bin/env python3
"""Generic state engine for agentic loops.

One engine serves every flow. It reads a flow definition (flow.json), keeps one
run's progress in <runs-dir>/<flow>/<run>/state.json and tells the orchestrator
the single next thing to do. Agents never edit state.json themselves: they call
this CLI, which validates every transition, writes atomically and appends each
event to history.jsonl next to the state.

Stdlib only (Python 3.9+). Every command prints JSON except `status`.

    state_manager.py --run PROJ-123 init --input story=PROJ-123
    state_manager.py --run PROJ-123 next
    state_manager.py --run PROJ-123 start    <step>
    state_manager.py --run PROJ-123 complete <step> [--agent ID] [--artifact PATH ...] [--summary TEXT]
    state_manager.py --run PROJ-123 poll     <step> [--block SECONDS] [--now]
    state_manager.py --run PROJ-123 approve  <gate-or-step> [--note TEXT]
    state_manager.py --run PROJ-123 reject   <gate-or-step> --feedback TEXT
    state_manager.py --run PROJ-123 status
    state_manager.py preflight

Step paths are `step_id` for single steps and `step_id[ITEM].cycle_step_id` for
the cycle steps of a foreach, e.g. `subtasks[T2].implement`.

What a step can declare besides its skill and artifacts:

- `agents`: several subagents in parallel; each reports with `complete --agent ID`.
- artifact contracts (`sections`, `contains`, `min_bytes`, `json_schema`) and
  `checks` (commands that must exit 0): `complete` refuses output that fails them.
- `when`: a condition; the step is skipped when it is false.
- `repeat`: after the step, a condition decides whether the work loops back.
- `wait`: the step waits for something external (CI, a review, a deploy),
  polled by a command or a subagent, with an interval and a timeout.
- `requires`: commands, environment variables, MCP servers and tools the step
  needs; `preflight` (also run by `init`) checks them before anything starts.
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple, Optional

try:
    import fcntl
except ImportError:  # Windows: no advisory locking, single orchestrator assumed.
    fcntl = None  # type: ignore[assignment]

STATE_VERSION = 1
DEFAULT_MAX_ITERATIONS = 3
DEFAULT_MAX_FIX_ATTEMPTS = 3
DEFAULT_RUNS_DIR = ".agentic-loops"
DEFAULT_FLOW = Path(__file__).resolve().with_name("flow.json")
DEFAULT_POLL_EVERY = 300
DEFAULT_CHECK_TIMEOUT = 600
MAX_BLOCK_SECONDS = 590  # stays under a 10-minute tool timeout

DONE = ("done", "skipped")
UNIT_STATUSES = ("pending", "running", "waiting", "awaiting_gate", "done", "skipped", "failed")

FLOW_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PATH_RE = re.compile(
    r"^(?P<step>[a-z][a-z0-9_]*)(?:\[(?P<item>[A-Za-z0-9][A-Za-z0-9_.-]*)\]\.(?P<sub>[a-z][a-z0-9_]*))?$"
)

WAIT_STATUS_SCHEMA = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"enum": ["ready", "pending", "failed"]}, "summary": {"type": "string"}},
}

# Clock and sleep are module attributes so tests can control time.
_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
_sleep: Callable[[float], None] = time.sleep


class EngineError(Exception):
    """A refused command. The message is written for the agent that sent it."""


class VerificationError(EngineError):
    """Output was refused by its contract or checks. State changed (a try was
    counted, or the step failed), so the caller must still save the run."""


def now() -> str:
    return _clock().isoformat(timespec="seconds")


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def parse_duration(value: Any) -> int:
    """Seconds from 90, "90s", "5m", "2h" or "1d"."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", str(value))
    if not m:
        raise ValueError(f"bad duration {value!r}; use seconds or e.g. '30s', '5m', '2h', '1d'")
    return int(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise EngineError(f"{what} not found: {path}") from None
    except json.JSONDecodeError as e:
        raise EngineError(f"{what} is not valid JSON ({path}): {e}") from None


def _tail(text: str, lines: int = 30, chars: int = 4000) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])[-chars:]


# --------------------------------------------------------------------------- #
# Flow definition
# --------------------------------------------------------------------------- #

def load_flow(path: Path) -> dict:
    flow = read_json(path, "flow definition")
    errors = validate_flow(flow)
    if errors:
        raise EngineError("invalid flow definition:\n- " + "\n- ".join(errors))
    return flow


def flow_schemas(flow: dict) -> dict:
    """Named JSON Schemas: `schemas`, plus the older `item_schemas`."""
    return {**flow.get("item_schemas", {}), **flow.get("schemas", {})}


def iter_units(flow: dict) -> Iterator[tuple[dict, dict]]:
    """(step, unit) pairs: a single step is its own unit; a foreach yields its cycle steps."""
    for sdef in flow["steps"]:
        for udef in sdef.get("cycle", [sdef]):
            yield sdef, udef


def validate_flow(flow: Any) -> list[str]:
    """Return every structural problem in a flow definition (empty when valid)."""
    if not isinstance(flow, dict):
        return ["flow must be a JSON object"]
    errors: list[str] = []
    if not isinstance(flow.get("name"), str) or not FLOW_NAME_RE.match(flow["name"]):
        errors.append("'name' must be kebab-case, e.g. 'development-loop'")
    if not isinstance(flow.get("inputs", {}), dict):
        errors.append("'inputs' must map input names to descriptions")
    for key in ("item_schemas", "schemas"):
        value = flow.get(key, {})
        if not isinstance(value, dict) or not all(isinstance(s, dict) for s in value.values()):
            errors.append(f"'{key}' must map names to JSON Schemas")
    schemas = {k: v for key in ("item_schemas", "schemas") if isinstance(flow.get(key), dict)
               for k, v in flow[key].items()}
    defaults = flow.get("defaults", {})
    if not isinstance(defaults, dict):
        errors.append("'defaults' must be an object")
        defaults = {}
    _check_options(defaults, "defaults", errors)
    orch = flow.get("orchestrator", {})
    if not isinstance(orch, dict) or not all(isinstance(v, str) for v in orch.values()):
        errors.append("'orchestrator' must map agent settings (e.g. model, color) to strings")
    _check_subagents(flow, errors)
    _check_requires(flow.get("requires"), "flow", errors)

    steps = flow.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("'steps' must be a non-empty list")
        return errors

    seen: dict[str, dict] = {}
    gate_ids: set[str] = set()
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or not isinstance(step.get("id"), str) or not ID_RE.match(step["id"]):
            errors.append(f"steps[{i}]: 'id' must be snake_case, e.g. 'jira_context'")
            continue
        sid = step["id"]
        where = f"step '{sid}'"
        allowed = FOREACH_KEYS if step.get("kind") == "foreach" else STEP_KEYS
        for key in sorted(set(step) - allowed):
            errors.append(f"{where}: unknown key '{key}'" + _did_you_mean(key, allowed))
        if sid in seen:
            errors.append(f"{where}: duplicate id")
        for key in ("needs", "inputs"):
            refs = step.get(key, [])
            if not isinstance(refs, list):
                errors.append(f"{where}: '{key}' must be a list of step ids")
                continue
            for ref in refs:
                if ref not in seen:
                    errors.append(f"{where}: {key} '{ref}' is not an earlier step (steps run in listed order)")
        _check_options(step, where, errors)
        kind = step.get("kind", "single")
        if kind == "single":
            _check_unit(step, where, errors, gate_ids, schemas)
            earlier_single = {s for s, d in seen.items() if d.get("kind", "single") == "single"} | {sid}
            for label, target in _back_targets(step):
                if target not in earlier_single:
                    errors.append(f"{where}: {label} '{target}' must be this step or an earlier single step")
            pi = step.get("produces_items")
            if pi is not None:
                if not isinstance(pi, dict) or not isinstance(pi.get("file"), str):
                    errors.append(f"{where}: 'produces_items' needs a 'file'")
                else:
                    if pi["file"] not in unit_artifacts(step):
                        errors.append(f"{where}: produces_items file '{pi['file']}' must be listed in 'artifacts'")
                    if "item_schema" in pi and pi["item_schema"] not in schemas:
                        errors.append(f"{where}: item_schema '{pi['item_schema']}' is not defined in 'schemas'")
        elif kind == "foreach":
            over = step.get("over")
            if over not in seen:
                errors.append(f"{where}: 'over' must name an earlier step")
            elif not seen[over].get("produces_items"):
                errors.append(f"{where}: 'over' step '{over}' has no 'produces_items'")
            for key in ("gate", "produces_items", "repeat", "wait", "checks", "agents"):
                if key in step:
                    errors.append(f"{where}: a foreach step cannot have '{key}'; put it on a cycle step")
            if "when" in step:
                _check_condition(step["when"], f"{where} when", errors)
            cycle = step.get("cycle")
            if not isinstance(cycle, list) or not cycle:
                errors.append(f"{where}: 'cycle' must be a non-empty list of steps")
                cycle = []
            sub_ids: list[str] = []
            for j, sub in enumerate(cycle):
                if not isinstance(sub, dict) or not isinstance(sub.get("id"), str) or not ID_RE.match(sub["id"]):
                    errors.append(f"{where}: cycle[{j}] 'id' must be snake_case")
                    continue
                sub_where = f"{where} cycle step '{sub['id']}'"
                for key in sorted(set(sub) - CYCLE_KEYS):
                    errors.append(f"{sub_where}: unknown key '{key}'" + _did_you_mean(key, CYCLE_KEYS))
                if sub["id"] in sub_ids:
                    errors.append(f"{sub_where}: duplicate id")
                _check_options(sub, sub_where, errors)
                _check_unit(sub, sub_where, errors, gate_ids, schemas, in_cycle=True)
                for label, target in _back_targets(sub):
                    if target not in sub_ids + [sub["id"]]:
                        errors.append(f"{sub_where}: {label} '{target}' must be this or an earlier cycle step")
                sub_ids.append(sub["id"])
        else:
            errors.append(f"{where}: unknown kind '{kind}' (use 'single' or 'foreach')")
        seen[sid] = step
    return errors


CYCLE_KEYS = {"id", "description", "skill", "artifacts", "gate", "agents", "run_in_subagent", "subagent_type",
              "max_iterations", "max_fix_attempts", "when", "repeat", "wait", "checks", "requires"}
STEP_KEYS = CYCLE_KEYS | {"kind", "needs", "inputs", "produces_items"}
FOREACH_KEYS = {"id", "kind", "description", "needs", "inputs", "over", "cycle", "respect_item_dependencies",
                "max_iterations", "max_fix_attempts", "run_in_subagent", "subagent_type", "when"}


def _did_you_mean(key: str, allowed: set[str]) -> str:
    close = difflib.get_close_matches(key, sorted(allowed), n=1)
    return f" (did you mean '{close[0]}'?)" if close else ""


def _back_targets(unit: dict) -> list[tuple[str, str]]:
    """Every place a unit can send work back to, as (label, target id)."""
    out = []
    gate = unit.get("gate")
    if isinstance(gate, dict) and "on_reject" in gate:
        out.append(("gate on_reject", gate["on_reject"]))
    rep = unit.get("repeat")
    if isinstance(rep, dict) and "back_to" in rep:
        out.append(("repeat back_to", rep["back_to"]))
    wait = unit.get("wait")
    if isinstance(wait, dict) and isinstance(wait.get("on_failure"), dict) and "back_to" in wait["on_failure"]:
        out.append(("wait on_failure back_to", wait["on_failure"]["back_to"]))
    return out


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _check_options(obj: dict, where: str, errors: list[str]) -> None:
    for key in ("max_iterations", "max_fix_attempts"):
        if key in obj and not _positive_int(obj[key]):
            errors.append(f"{where}: '{key}' must be a positive integer")
    if "run_in_subagent" in obj and not isinstance(obj["run_in_subagent"], bool):
        errors.append(f"{where}: 'run_in_subagent' must be true or false")
    if "subagent_type" in obj and (not isinstance(obj["subagent_type"], str) or not obj["subagent_type"]):
        errors.append(f"{where}: 'subagent_type' must be an agent name, e.g. 'general-purpose'")


# Frontmatter fields a flow may set on a custom subagent it defines.
SUBAGENT_FIELDS = {
    "description": str, "tools": (str, list), "disallowedTools": (str, list), "model": str, "color": str,
    "effort": str, "maxTurns": int, "permissionMode": str, "isolation": str, "skills": list, "mcpServers": list,
}


def _check_subagents(flow: dict, errors: list[str]) -> None:
    subagents = flow.get("subagents", {})
    if not isinstance(subagents, dict):
        errors.append("'subagents' must map agent names to their settings")
        return
    for name, spec in subagents.items():
        where = f"subagent '{name}'"
        if not FLOW_NAME_RE.match(name):
            errors.append(f"{where}: name must be kebab-case, e.g. 'development-loop-implementer'")
        if name == flow.get("name"):
            errors.append(f"{where}: the flow's own name is reserved for its orchestrator agent")
        if not isinstance(spec, dict):
            errors.append(f"{where}: settings must be an object")
            continue
        if not isinstance(spec.get("description"), str) or not spec["description"].strip():
            errors.append(f"{where}: 'description' is required (when Claude should use this agent)")
        for key, value in spec.items():
            if key not in SUBAGENT_FIELDS:
                errors.append(f"{where}: unknown field '{key}' (allowed: {', '.join(sorted(SUBAGENT_FIELDS))})")
            elif not isinstance(value, SUBAGENT_FIELDS[key]) or isinstance(value, bool) or (
                    isinstance(value, list) and key != "mcpServers" and not all(isinstance(v, str) for v in value)):
                errors.append(f"{where}: '{key}' has the wrong type")


REQUIRE_KEYS = ("commands", "env", "mcp", "tools")


def _check_requires(req: Any, where: str, errors: list[str]) -> None:
    if req is None:
        return
    if not isinstance(req, dict):
        errors.append(f"{where}: 'requires' must be an object with {', '.join(REQUIRE_KEYS)}")
        return
    for key, value in req.items():
        if key not in REQUIRE_KEYS:
            errors.append(f"{where}: unknown requires key '{key}' (use {', '.join(REQUIRE_KEYS)})")
        elif not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            errors.append(f"{where}: requires.{key} must be a list of names")


# ---- artifacts and their contracts ----------------------------------------- #

ARTIFACT_KEYS = {"path", "description", "sections", "contains", "min_bytes", "json_schema", "optional"}


def norm_artifact(a: Any) -> dict:
    return {"path": a} if isinstance(a, str) else dict(a)


def _valid_rel_path(p: Any) -> bool:
    return isinstance(p, str) and bool(p) and not p.startswith("/") and ".." not in Path(p).parts


def _check_artifacts(arts: Any, where: str, errors: list[str], schemas: dict) -> list[str]:
    """Validate an artifacts list (strings or contract objects); return the paths."""
    if not isinstance(arts, list):
        errors.append(f"{where}: 'artifacts' must be a list")
        return []
    paths = []
    for a in arts:
        if isinstance(a, str):
            spec = {"path": a}
        elif isinstance(a, dict):
            spec = a
            unknown = set(a) - ARTIFACT_KEYS
            if unknown:
                errors.append(f"{where}: artifact has unknown keys {sorted(unknown)} (allowed: {', '.join(sorted(ARTIFACT_KEYS))})")
        else:
            errors.append(f"{where}: each artifact is a relative path or an object with a 'path'")
            continue
        if not _valid_rel_path(spec.get("path")):
            errors.append(f"{where}: artifact path {spec.get('path')!r} must be a relative file name")
            continue
        p = spec["path"]
        paths.append(p)
        for key in ("sections", "contains"):
            if key in spec and (not isinstance(spec[key], list) or not all(isinstance(s, str) and s for s in spec[key])):
                errors.append(f"{where}: {p}: '{key}' must be a list of strings")
        for pattern in spec.get("contains", []) if isinstance(spec.get("contains"), list) else []:
            try:
                re.compile(pattern)
            except re.error as e:
                errors.append(f"{where}: {p}: 'contains' pattern {pattern!r} is not a valid regex: {e}")
        if "min_bytes" in spec and not _positive_int(spec["min_bytes"]):
            errors.append(f"{where}: {p}: 'min_bytes' must be a positive integer")
        if "optional" in spec and not isinstance(spec["optional"], bool):
            errors.append(f"{where}: {p}: 'optional' must be true or false")
        js = spec.get("json_schema")
        if js is not None and not isinstance(js, dict) and js not in schemas:
            errors.append(f"{where}: {p}: json_schema {js!r} is neither a schema object nor a name in 'schemas'")
    return paths


def artifact_specs(unit_def: dict, agent_def: Optional[dict] = None) -> list[dict]:
    """Normalized artifact contracts of a step, or of one of its parallel agents
    (its own list, or the step's list with {agent} replaced by the agent id)."""
    if agent_def is not None:
        if "artifacts" in agent_def:
            return [norm_artifact(a) for a in agent_def["artifacts"]]
        return [{**s, "path": s["path"].replace("{agent}", agent_def["id"])}
                for s in map(norm_artifact, unit_def.get("artifacts", []))]
    if unit_def.get("agents"):
        return [s for ad in unit_def["agents"] for s in artifact_specs(unit_def, ad)]
    return [norm_artifact(a) for a in unit_def.get("artifacts", [])]


def agent_artifacts(unit_def: dict, agent_def: dict) -> list[str]:
    return [s["path"] for s in artifact_specs(unit_def, agent_def)]


def unit_artifacts(unit_def: dict) -> list[str]:
    """Every artifact path a step produces (all agents' for a parallel step)."""
    return [s["path"] for s in artifact_specs(unit_def)]


def wait_kind(unit_def: dict) -> Optional[str]:
    """'command' (the engine runs the check), 'agent' (a subagent checks), or None."""
    wait = unit_def.get("wait")
    if not isinstance(wait, dict):
        return None
    return "agent" if wait.get("check") == "agent" else "command"


def wait_status_path(unit_def: dict, in_cycle: bool) -> str:
    wait = unit_def.get("wait") or {}
    if wait.get("status_file"):
        return wait["status_file"]
    return f"{{item}}/{unit_def['id']}.status.json" if in_cycle else f"{unit_def['id']}.status.json"


def _check_unit(unit: dict, where: str, errors: list[str], gate_ids: set[str], schemas: dict,
                in_cycle: bool = False) -> None:
    kind = wait_kind(unit)
    if kind != "command" and (not isinstance(unit.get("skill"), str) or not unit["skill"]):
        errors.append(f"{where}: 'skill' (path of the step's instructions) is required")
    arts = _check_artifacts(unit.get("artifacts", []), where, errors, schemas)
    _check_requires(unit.get("requires"), where, errors)
    if "when" in unit:
        _check_condition(unit["when"], f"{where} when", errors)
    _check_checks(unit.get("checks"), where, errors)
    _check_repeat(unit, where, errors)
    if "wait" in unit:
        _check_wait(unit, where, errors, in_cycle)
    if in_cycle is False and any("{item}" in a for a in arts):
        errors.append(f"{where}: '{{item}}' in artifact names only works in a foreach cycle")
    agents = unit.get("agents")
    if agents is None:
        if any("{agent}" in a for a in arts):
            errors.append(f"{where}: '{{agent}}' in artifact names needs an 'agents' list")
    elif not isinstance(agents, list) or not agents:
        errors.append(f"{where}: 'agents' must be a non-empty list of parallel subagents")
    else:
        if unit.get("run_in_subagent") is False:
            errors.append(f"{where}: parallel 'agents' always run as subagents; drop run_in_subagent: false")
        for key in ("produces_items", "checks", "wait"):
            if key in unit:
                errors.append(f"{where}: a step with parallel 'agents' cannot have '{key}'")
        agent_ids: list[str] = []
        for k, ad in enumerate(agents):
            if not isinstance(ad, dict) or not isinstance(ad.get("id"), str) or not ID_RE.match(ad["id"]):
                errors.append(f"{where}: agents[{k}] 'id' must be snake_case")
                continue
            awhere = f"{where} agent '{ad['id']}'"
            if ad["id"] in agent_ids:
                errors.append(f"{awhere}: duplicate id")
            agent_ids.append(ad["id"])
            if "skill" in ad and (not isinstance(ad["skill"], str) or not ad["skill"]):
                errors.append(f"{awhere}: 'skill' must be a path")
            if "focus" in ad and not isinstance(ad["focus"], str):
                errors.append(f"{awhere}: 'focus' must be text")
            _check_requires(ad.get("requires"), awhere, errors)
            if "artifacts" in ad:
                if not _check_artifacts(ad["artifacts"], awhere, errors, schemas):
                    continue
            _check_options({k: v for k, v in ad.items() if k == "subagent_type"}, awhere, errors)
            if not agent_artifacts(unit, ad):
                errors.append(f"{awhere}: produces no artifacts; give it 'artifacts' or use '{{agent}}' in the step's")
        if all(isinstance(ad, dict) and isinstance(ad.get("id"), str) for ad in agents):
            names = unit_artifacts(unit)
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                errors.append(f"{where}: parallel agents would write the same file ({', '.join(dupes)}); "
                              "put '{agent}' in the step's artifact names or give each agent its own")
            arts = names
    gate = unit.get("gate")
    if gate is None:
        return
    if not isinstance(gate, dict) or not isinstance(gate.get("id"), str) or not gate["id"]:
        errors.append(f"{where}: a gate needs an 'id'")
        return
    if gate["id"] in gate_ids:
        errors.append(f"{where}: gate id '{gate['id']}' is used twice")
    gate_ids.add(gate["id"])
    when = gate.get("when", "after")
    if when not in ("after", "before"):
        errors.append(f"{where}: gate 'when' must be 'after' or 'before'")
    if when == "after" and not arts and not kind:
        errors.append(f"{where}: gate {gate['id']} reviews the step's output, but the step declares no artifacts")
    guard = gate.get("guard")
    if guard is not None:
        if when != "before":
            errors.append(f"{where}: a guard only makes sense on a 'before' gate")
        elif not isinstance(guard, dict) or not isinstance(guard.get("pattern"), str):
            errors.append(f"{where}: guard needs a regex 'pattern' (and optionally a 'tool')")
        else:
            try:
                re.compile(guard["pattern"])
            except re.error as e:
                errors.append(f"{where}: guard pattern is not a valid regex: {e}")


def _check_checks(checks: Any, where: str, errors: list[str]) -> None:
    if checks is None:
        return
    if not isinstance(checks, list):
        errors.append(f"{where}: 'checks' must be a list of commands")
        return
    for c in checks:
        if isinstance(c, str) and c.strip():
            continue
        if isinstance(c, dict) and isinstance(c.get("run"), str) and c["run"].strip():
            if set(c) - {"run", "name", "timeout"}:
                errors.append(f"{where}: a check takes 'run', 'name' and 'timeout'")
            if "timeout" in c:
                _check_duration(c["timeout"], f"{where} check timeout", errors)
            continue
        errors.append(f"{where}: each check is a command string or {{\"run\": ..., \"name\": ...}}")


def _check_duration(value: Any, where: str, errors: list[str]) -> None:
    try:
        parse_duration(value)
    except ValueError as e:
        errors.append(f"{where}: {e}")


def _check_repeat(unit: dict, where: str, errors: list[str]) -> None:
    rep = unit.get("repeat")
    if rep is None:
        return
    if not isinstance(rep, dict) or "until" not in rep:
        errors.append(f"{where}: 'repeat' needs an 'until' condition")
        return
    unknown = set(rep) - {"until", "back_to", "max", "on_exhausted", "feedback"}
    if unknown:
        errors.append(f"{where}: repeat has unknown keys {sorted(unknown)}")
    _check_condition(rep["until"], f"{where} repeat.until", errors)
    if "max" in rep and not _positive_int(rep["max"]):
        errors.append(f"{where}: repeat.max must be a positive integer (the most rounds to run)")
    on = rep.get("on_exhausted", "fail")
    if on not in ("fail", "gate", "continue"):
        errors.append(f"{where}: repeat.on_exhausted must be 'fail', 'gate' or 'continue'")
    if on == "gate" and (not isinstance(unit.get("gate"), dict) or unit["gate"].get("when", "after") != "after"):
        errors.append(f"{where}: repeat.on_exhausted 'gate' needs an 'after' gate on this step")
    if "feedback" in rep and (not isinstance(rep["feedback"], list) or not all(_valid_rel_path(p) for p in rep["feedback"])):
        errors.append(f"{where}: repeat.feedback must be a list of artifact paths")


def _check_wait(unit: dict, where: str, errors: list[str], in_cycle: bool) -> None:
    wait = unit["wait"]
    if not isinstance(wait, dict):
        errors.append(f"{where}: 'wait' must be an object")
        return
    unknown = set(wait) - {"check", "poll_every", "timeout", "on_failure", "on_timeout", "status_file", "description"}
    if unknown:
        errors.append(f"{where}: wait has unknown keys {sorted(unknown)}")
    check = wait.get("check")
    if check == "agent":
        pass
    elif isinstance(check, dict) and isinstance(check.get("run"), str) and check["run"].strip():
        for key in ("ready_exit_codes", "failed_exit_codes"):
            if key in check and (not isinstance(check[key], list) or not all(isinstance(c, int) for c in check[key])):
                errors.append(f"{where}: wait.check.{key} must be a list of exit codes")
        if "timeout" in check:
            _check_duration(check["timeout"], f"{where} wait.check.timeout", errors)
    else:
        errors.append(f"{where}: wait.check must be {{\"run\": \"<command>\"}} or \"agent\"")
    for key in ("poll_every", "timeout"):
        if key in wait:
            _check_duration(wait[key], f"{where} wait.{key}", errors)
    on_failure = wait.get("on_failure", "fail")
    if on_failure != "fail" and not (isinstance(on_failure, dict) and isinstance(on_failure.get("back_to"), str)):
        errors.append(f"{where}: wait.on_failure must be 'fail' or {{\"back_to\": \"<step>\"}}")
    if wait.get("on_timeout", "fail") not in ("fail", "continue"):
        errors.append(f"{where}: wait.on_timeout must be 'fail' or 'continue'")
    if "status_file" in wait:
        if not _valid_rel_path(wait["status_file"]):
            errors.append(f"{where}: wait.status_file must be a relative path")
        elif in_cycle and "{item}" not in wait["status_file"]:
            errors.append(f"{where}: in a cycle, wait.status_file must contain '{{item}}'")


# ---- conditions ------------------------------------------------------------- #

COND_OPS = {"equals": "==", "not_equals": "!=", "in": "in", "not_in": "not in",
            "matches": "matches", "gte": ">=", "lte": "<=", "exists": "exists"}


def _check_condition(cond: Any, where: str, errors: list[str]) -> None:
    """Conditions are declarative so the engine can evaluate them:
    {"all": [...]}, {"any": [...]}, {"not": cond}, or a test on a source:
    {"file": "review.json", "field": "verdict", "equals": "PASS"} (field is a
    dotted path into JSON; without it the test applies to the file's text) or
    {"input": "story_type", "in": ["bug", "task"]}. Ops: equals, not_equals,
    in, not_in, matches (regex), gte, lte, exists (true/false)."""
    if not isinstance(cond, dict) or not cond:
        errors.append(f"{where}: a condition must be an object")
        return
    for combinator in ("all", "any"):
        if combinator in cond:
            if set(cond) != {combinator} or not isinstance(cond[combinator], list) or not cond[combinator]:
                errors.append(f"{where}: '{combinator}' takes a non-empty list of conditions and nothing else")
                return
            for k, c in enumerate(cond[combinator]):
                _check_condition(c, f"{where}.{combinator}[{k}]", errors)
            return
    if "not" in cond:
        if set(cond) != {"not"}:
            errors.append(f"{where}: 'not' takes one condition and nothing else")
            return
        _check_condition(cond["not"], f"{where}.not", errors)
        return
    sources = [k for k in ("file", "input") if k in cond]
    if len(sources) != 1:
        errors.append(f"{where}: give exactly one of 'file' or 'input'")
        return
    if "file" in cond and not _valid_rel_path(cond["file"]):
        errors.append(f"{where}: 'file' must be a path relative to the run's artifacts")
    if "field" in cond and (not isinstance(cond["field"], str) or not cond["field"]):
        errors.append(f"{where}: 'field' must be a dotted path such as 'verdict' or 'checks.0.state'")
    ops = [k for k in cond if k in COND_OPS]
    extra = set(cond) - set(ops) - {"file", "input", "field"}
    if len(ops) != 1 or extra:
        errors.append(f"{where}: use exactly one test of {', '.join(COND_OPS)}")
        return
    op, value = ops[0], cond[ops[0]]
    if op in ("in", "not_in") and not isinstance(value, list):
        errors.append(f"{where}: '{op}' takes a list")
    if op in ("gte", "lte") and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        errors.append(f"{where}: '{op}' takes a number")
    if op == "exists" and not isinstance(value, bool):
        errors.append(f"{where}: 'exists' takes true or false")
    if op == "matches":
        try:
            re.compile(value)
        except (re.error, TypeError) as e:
            errors.append(f"{where}: 'matches' is not a valid regex: {e}")


def describe_condition(cond: dict) -> str:
    if "all" in cond:
        return "(" + " and ".join(describe_condition(c) for c in cond["all"]) + ")"
    if "any" in cond:
        return "(" + " or ".join(describe_condition(c) for c in cond["any"]) + ")"
    if "not" in cond:
        return "not " + describe_condition(cond["not"])
    src = f"input '{cond['input']}'" if "input" in cond else cond["file"]
    if cond.get("field"):
        src += f" → {cond['field']}"
    op = next(k for k in cond if k in COND_OPS)
    if op == "exists":
        return f"{src} {'exists' if cond[op] else 'does not exist'}"
    return f"{src} {COND_OPS[op]} {json.dumps(cond[op])}"


def _dig(data: Any, field: str) -> tuple[bool, Any]:
    for part in field.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.lstrip("-").isdigit() and -len(data) <= int(part) < len(data):
            data = data[int(part)]
        else:
            return False, None
    return True, data


def _compare(op: str, value: Any, expected: Any) -> bool:
    if op == "equals":
        return value == expected
    if op == "not_equals":
        return value != expected
    if op == "in":
        return value in expected
    if op == "not_in":
        return value not in expected
    if op == "matches":
        return re.search(expected, value if isinstance(value, str) else json.dumps(value), re.M) is not None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number >= expected if op == "gte" else number <= expected


# --------------------------------------------------------------------------- #
# JSON Schema subset, items
# --------------------------------------------------------------------------- #

_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def schema_errors(value: Any, schema: dict, where: str = "$") -> list[str]:
    """Validate against the JSON Schema subset flows use: type, enum, const,
    required, properties, additionalProperties: false, items, minItems,
    minLength, minimum, maximum, pattern."""
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_TYPES.get(x, lambda v: True)(value) for x in types):
            return [f"{where}: expected {' or '.join(types)}"]
    errors = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: must be one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: must be {json.dumps(schema['const'])}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{where}: must have at least {schema['minLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{where}: must match /{schema['pattern']}/")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where}: must be <= {schema['maximum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{where}: needs at least {schema['minItems']} entries")
        if isinstance(schema.get("items"), dict):
            for i, v in enumerate(value):
                errors += schema_errors(v, schema["items"], f"{where}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where}: missing required field '{key}'")
        for key, v in value.items():
            if key in props:
                errors += schema_errors(v, props[key], f"{where}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{where}: unexpected field '{key}'")
    return errors


def check_items(flow: dict, pi: dict, data: Any) -> list[dict]:
    """Validate a produced items file and return its items, or raise EngineError."""
    key = pi.get("key")
    if key:
        data = data.get(key) if isinstance(data, dict) else None
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        data = data["items"]
    if not isinstance(data, list):
        where = f"a list under '{key}'" if key else "a JSON list (or an object with an 'items' list)"
        raise EngineError(f"{pi['file']} must contain {where}")
    id_field = pi.get("id_field", "id")
    dep_field = pi.get("depends_on_field", "depends_on")
    schema = flow_schemas(flow).get(pi["item_schema"]) if pi.get("item_schema") else None
    errors: list[str] = []
    if not data:
        errors.append("it contains no items")
    ids: list[str] = []
    for i, item in enumerate(data):
        where = f"item[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: must be an object")
            continue
        if schema:
            errors += schema_errors(item, schema, where)
        iid = item.get(id_field)
        if not isinstance(iid, str) or not ITEM_ID_RE.match(iid):
            errors.append(f"{where}: '{id_field}' must be a short string id such as 'T1'")
            continue
        if iid in ids:
            errors.append(f"{where}: duplicate id '{iid}'")
        ids.append(iid)
    graph: dict[str, list[str]] = {}
    for item in data:
        if not isinstance(item, dict) or item.get(id_field) not in ids:
            continue
        deps = item.get(dep_field, [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            errors.append(f"item '{item[id_field]}': '{dep_field}' must be a list of item ids")
            continue
        for d in deps:
            if d == item[id_field]:
                errors.append(f"item '{d}' depends on itself")
            elif d not in ids:
                errors.append(f"item '{item[id_field]}' depends on unknown item '{d}'")
        graph[item[id_field]] = deps
    if not errors:
        cycle = _find_cycle(graph)
        if cycle:
            errors.append("dependency cycle: " + " -> ".join(cycle))
    if errors:
        raise EngineError(f"{pi['file']} is invalid:\n- " + "\n- ".join(errors))
    return data


def _find_cycle(graph: dict[str, list[str]]) -> Optional[list[str]]:
    color: dict[str, int] = {}
    stack: list[str] = []

    def visit(n: str) -> Optional[list[str]]:
        color[n] = 1
        stack.append(n)
        for d in graph.get(n, []):
            if color.get(d) == 1:
                return stack[stack.index(d):] + [d]
            if color.get(d) is None:
                found = visit(d)
                if found:
                    return found
        stack.pop()
        color[n] = 2
        return None

    for n in graph:
        if color.get(n) is None:
            found = visit(n)
            if found:
                return found
    return None


# --------------------------------------------------------------------------- #
# Describing what will be verified (shown to subagents and in step stubs)
# --------------------------------------------------------------------------- #

def describe_artifact(spec: dict) -> str:
    parts = []
    if spec.get("optional"):
        parts.append("optional")
    if spec.get("sections"):
        parts.append("must have the headings " + ", ".join(f"`{s}`" for s in spec["sections"]))
    if spec.get("contains"):
        parts.append("must match " + ", ".join(f"/{c}/" for c in spec["contains"]))
    if spec.get("min_bytes"):
        parts.append(f"at least {spec['min_bytes']} bytes")
    if spec.get("json_schema") is not None:
        js = spec["json_schema"]
        parts.append(f"valid JSON matching the `{js}` schema" if isinstance(js, str) else "valid JSON matching its schema")
    return f"`{spec['path']}`" + (": " + "; ".join(parts) if parts else ": must exist")


def check_command(c: Any) -> str:
    return c if isinstance(c, str) else c["run"]


def describe_contract(unit_def: dict, agent_def: Optional[dict] = None) -> list[str]:
    """What `complete` will verify, and what happens after, in plain sentences."""
    lines = [describe_artifact(s) for s in artifact_specs(unit_def, agent_def)]
    pi = unit_def.get("produces_items")
    if pi:
        lines.append(f"`{pi['file']}` must hold valid items"
                     + (f" matching the `{pi['item_schema']}` schema" if pi.get("item_schema") else "")
                     + "; ids unique, `depends_on` only naming existing items, no cycles")
    for c in unit_def.get("checks", []) if agent_def is None else []:
        lines.append(f"the command `{check_command(c)}` must exit 0")
    rep = unit_def.get("repeat")
    if rep:
        target = rep.get("back_to", unit_def["id"])
        lines.append(f"afterwards the flow checks {describe_condition(rep['until'])}; "
                     f"if that is false the work goes back to `{target}` (at most {rep.get('max', DEFAULT_MAX_ITERATIONS)} rounds)")
    return lines


# --------------------------------------------------------------------------- #
# Requirements and preflight
# --------------------------------------------------------------------------- #

_SHELL_WORDS = {"test", "[", "cd", "true", "false", "echo", "exit", "export", "set", "if", "for", "while",
                "!", "{", "(", "printf", "read", "eval", "command", "env"}


def _command_words(cmd: str) -> list[str]:
    """Executables named at the start of each pipeline segment of a command."""
    words = []
    for segment in re.split(r"&&|\|\||[;|]", cmd):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        tokens = [t for t in tokens if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]
        if tokens and tokens[0] not in _SHELL_WORDS and not tokens[0].startswith("$"):
            words.append(tokens[0])
    return words


def flow_requirements(flow: dict) -> dict[str, list[str]]:
    """Everything a flow needs from the environment: its declared `requires`
    plus the executables its checks and wait commands call."""
    req: dict[str, list[str]] = {k: [] for k in REQUIRE_KEYS}

    def add(source: Optional[dict]) -> None:
        for k in REQUIRE_KEYS:
            req[k] += (source or {}).get(k, [])

    add(flow.get("requires"))
    for _, udef in iter_units(flow):
        add(udef.get("requires"))
        for ad in udef.get("agents", []):
            add(ad.get("requires"))
        commands = [check_command(c) for c in udef.get("checks", [])]
        if wait_kind(udef) == "command":
            commands.append(udef["wait"]["check"]["run"])
        for cmd in commands:
            req["commands"] += [w for w in _command_words(cmd) if "/" not in w]
    return {k: list(dict.fromkeys(v)) for k, v in req.items()}


def _configured_mcp_servers(project_dir: Path) -> set[str]:
    names: set[str] = set()
    candidates = [project_dir / ".mcp.json", project_dir / ".claude" / "settings.json",
                  project_dir / ".claude" / "settings.local.json", Path.home() / ".claude.json"]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        names |= set((data.get("mcpServers") or {}).keys())
        project = (data.get("projects") or {}).get(str(project_dir.resolve())) or {}
        names |= set((project.get("mcpServers") or {}).keys())
        names |= set(data.get("enabledMcpjsonServers") or [])
    return names


def preflight(flow: dict, project_dir: Optional[Path] = None) -> dict:
    """Check the environment before a run: commands on PATH, environment
    variables (an entry 'A|B' needs any one of them), MCP servers (best effort)."""
    project_dir = Path(project_dir or Path.cwd())
    req = flow_requirements(flow)
    missing, warnings = [], []
    for cmd in req["commands"]:
        if shutil.which(cmd) is None:
            missing.append(f"command `{cmd}` is not on PATH")
    for entry in req["env"]:
        options = entry.split("|")
        if not any(os.environ.get(v) for v in options):
            missing.append(f"environment variable {' or '.join(options)} is not set")
    if req["mcp"]:
        configured = _configured_mcp_servers(project_dir)
        for server in req["mcp"]:
            if server not in configured:
                warnings.append(f"could not confirm MCP server '{server}' is configured (checked .mcp.json, "
                                ".claude/settings*.json and ~/.claude.json); run `claude mcp list` to check")
    return {"ok": not missing, "missing": missing, "warnings": warnings, "requirements": req}


# --------------------------------------------------------------------------- #
# Run state
# --------------------------------------------------------------------------- #

def fresh_unit(unit_def: dict) -> dict:
    unit = {
        "status": "pending",  # see UNIT_STATUSES
        "attempt": 0,
        "artifacts": [],
        "previous_artifacts": [],
        "summary": None,
        "feedback": None,
        "feedback_files": [],
        "feedback_history": [],
        "error": None,
        "started_at": None,
        "completed_at": None,
        "loops": 0,          # automatic loop-backs made by this step's `repeat`
        "fix_failures": 0,   # refused completions in the current attempt
        "wait": None,        # polling state of a `wait` step
    }
    gate = unit_def.get("gate")
    if gate:
        unit["gate"] = {"id": gate["id"], "when": gate.get("when", "after"), "status": "pending", "decisions": []}
    if unit_def.get("agents"):
        unit["agents"] = {ad["id"]: fresh_agent() for ad in unit_def["agents"]}
    return unit


def fresh_agent() -> dict:
    return {"status": "pending", "artifacts": [], "summary": None, "error": None,
            "started_at": None, "completed_at": None, "fix_failures": 0}


def fresh_step_state(step_def: dict) -> dict:
    if step_def.get("kind") == "foreach":
        return {"kind": "foreach", "status": "pending", "items": None}
    return {"kind": "single", **fresh_unit(step_def)}


def _upgrade_unit(st: dict) -> None:
    """Add fields introduced after a state file was written."""
    for key, value in (("feedback_files", []), ("loops", 0), ("fix_failures", 0), ("wait", None)):
        st.setdefault(key, value)
    for ag in st.get("agents", {}).values():
        ag.setdefault("fix_failures", 0)


class Unit(NamedTuple):
    """One runnable thing: a single step, or one cycle step of one foreach item."""
    path: str
    step_def: dict
    unit_def: dict
    st: dict
    item: Optional[dict]


class Run:
    def __init__(self, flow: dict, flow_path: Path, run_dir: Path, state: dict, cli: Optional[str] = None):
        self.flow = flow
        self.flow_path = Path(flow_path).resolve()
        self.run_dir = Path(run_dir).resolve()
        self.state = state
        self.steps = {s["id"]: s for s in flow["steps"]}
        self.cli = cli or f"state_manager.py --run {state['run_id']}"
        self.sim: Optional[dict] = None  # set by simulate(): overrides for conditions and waits
        self._events: list[dict] = []
        for sid, sdef in self.steps.items():  # tolerate steps added to the flow mid-run
            st = self.state["steps"].setdefault(sid, fresh_step_state(sdef))
            if st["kind"] == "single":
                _upgrade_unit(st)
            for item in st.get("items") or []:
                for sub in item["steps"].values():
                    _upgrade_unit(sub)

    # ---- persistence ------------------------------------------------------ #

    @staticmethod
    def dir_for(flow: dict, runs_dir: Path, run_id: str) -> Path:
        if not isinstance(run_id, str) or not ITEM_ID_RE.match(run_id):
            raise EngineError(f"bad run id '{run_id}': use letters, digits, '.', '_' or '-', e.g. PROJ-123")
        return Path(runs_dir) / flow["name"] / run_id

    @classmethod
    def create(cls, flow_path: Path, runs_dir: Path, run_id: str, inputs: dict, cli: Optional[str] = None,
               check_environment: bool = False) -> "Run":
        flow = load_flow(flow_path)
        missing = [k for k in flow.get("inputs", {}) if k not in inputs]
        if missing:
            raise EngineError("missing inputs: " + ", ".join(f"--input {k}=..." for k in missing))
        run_dir = cls.dir_for(flow, runs_dir, run_id)
        if (run_dir / "state.json").exists():
            raise EngineError(f"run '{run_id}' already exists at {run_dir}; call `next` to resume it")
        if check_environment:
            report = preflight(flow)
            if not report["ok"]:
                raise EngineError("the environment is not ready for this flow (fix these, or pass --skip-preflight):\n- "
                                  + "\n- ".join(report["missing"]))
        state = {
            "version": STATE_VERSION,
            "flow": flow["name"],
            "run_id": run_id,
            "inputs": inputs,
            "project_dir": str(Path.cwd()),
            "created_at": now(),
            "updated_at": now(),
            "steps": {},
        }
        run = cls(flow, flow_path, run_dir, state, cli)
        run.artifacts_dir.mkdir(parents=True, exist_ok=True)
        run._event("init", inputs=inputs)
        run.save()
        return run

    @classmethod
    def load(cls, flow_path: Path, runs_dir: Path, run_id: str, cli: Optional[str] = None) -> "Run":
        flow = load_flow(flow_path)
        run_dir = cls.dir_for(flow, runs_dir, run_id)
        path = run_dir / "state.json"
        if not path.exists():
            raise EngineError(f"no run '{run_id}' (expected {path}); start it with `init`")
        state = read_json(path, "run state")
        if state.get("version") != STATE_VERSION:
            raise EngineError(f"{path} has state version {state.get('version')}, this engine reads {STATE_VERSION}")
        if state.get("flow") != flow["name"]:
            raise EngineError(f"{path} belongs to flow '{state.get('flow')}', not '{flow['name']}'")
        return cls(flow, flow_path, run_dir, state, cli)

    @property
    def artifacts_dir(self) -> Path:
        return self.run_dir / "artifacts"

    @property
    def state_path(self) -> Path:
        return self.run_dir / "state.json"

    @property
    def project_dir(self) -> Path:
        return Path(self.state.get("project_dir") or Path.cwd())

    @property
    def dirty(self) -> bool:
        return bool(self._events)

    def save(self) -> None:
        self.state["updated_at"] = now()
        atomic_write(self.state_path, json.dumps(self.state, indent=2) + "\n")
        if self._events:
            with open(self.run_dir / "history.jsonl", "a", encoding="utf-8") as f:
                for e in self._events:
                    f.write(json.dumps(e) + "\n")
            self._events.clear()

    def _event(self, event: str, **data: Any) -> None:
        self._events.append({"at": now(), "event": event, **data})

    # ---- lookups ------------------------------------------------------------ #

    def resolve(self, path: str) -> Unit:
        m = PATH_RE.match(path or "")
        if not m:
            raise EngineError(f"bad step path '{path}': use 'step' or 'step[ITEM].cycle_step'")
        sid = m["step"]
        sdef = self.steps.get(sid)
        if sdef is None:
            raise EngineError(f"unknown step '{sid}'; steps are: {', '.join(self.steps)}")
        st = self.state["steps"][sid]
        is_foreach = sdef.get("kind") == "foreach"
        if m["item"] is None:
            if is_foreach:
                raise EngineError(f"'{sid}' is a foreach; address one of its cycle steps, e.g. {sid}[ITEM].{sdef['cycle'][0]['id']}")
            return Unit(sid, sdef, sdef, st, None)
        if not is_foreach:
            raise EngineError(f"'{sid}' is not a foreach step; address it as '{sid}'")
        item = next((it for it in st["items"] or [] if it["id"] == m["item"]), None)
        if item is None:
            raise EngineError(f"'{sid}' has no item '{m['item']}'")
        sub = next((s for s in sdef["cycle"] if s["id"] == m["sub"]), None)
        if sub is None:
            raise EngineError(f"'{sid}' has no cycle step '{m['sub']}'")
        return Unit(path, sdef, sub, item["steps"][sub["id"]], item)

    def _art(self, name: str, unit: Optional[Unit]) -> str:
        return name.replace("{item}", unit.item["id"]) if unit and unit.item else name

    def _subagent_type(self, *sources: dict) -> str:
        for src in (*sources, self.flow.get("defaults", {})):
            if "subagent_type" in src:
                return src["subagent_type"]
        return "general-purpose"

    def _abs(self, p: str) -> str:
        return p if os.path.isabs(p) else str(self.artifacts_dir / p)

    def _setting(self, key: str, default: Any, *sources: dict) -> Any:
        for src in (*sources, self.flow.get("defaults", {})):
            if key in src:
                return src[key]
        return default

    def _max_iterations(self, unit_def: dict, step_def: dict) -> int:
        return self._setting("max_iterations", DEFAULT_MAX_ITERATIONS, unit_def, step_def)

    def _status_rel(self, unit: Unit) -> str:
        return self._art(wait_status_path(unit.unit_def, unit.item is not None), unit)

    def _step_artifacts(self, sid: str) -> list[str]:
        st = self.state["steps"][sid]
        if st["kind"] == "single":
            return list(st["artifacts"])
        out: list[str] = []
        for item in st["items"] or []:
            for sub in self.steps[sid]["cycle"]:
                out += item["steps"][sub["id"]]["artifacts"]
        return out

    def _inputs(self, unit: Unit) -> list[str]:
        sdef = unit.step_def
        refs = list(sdef.get("inputs", sdef.get("needs", [])))
        if sdef.get("kind") == "foreach" and sdef["over"] not in refs:
            refs.append(sdef["over"])
        paths: list[str] = []
        for ref in refs:
            paths += self._step_artifacts(ref)
        if unit.item:
            paths.append(unit.item["item_file"])
            idx = next(i for i, s in enumerate(sdef["cycle"]) if s["id"] == unit.unit_def["id"])
            for k, sub in enumerate(sdef["cycle"]):
                sst = unit.item["steps"][sub["id"]]
                if k < idx:
                    paths += sst["artifacts"]
                elif k > idx:  # e.g. the review that sent this implement step back
                    paths += sst["previous_artifacts"]
        paths += unit.st.get("feedback_files", [])
        return list(dict.fromkeys(self._abs(p) for p in paths))

    # ---- conditions --------------------------------------------------------- #

    def evaluate(self, cond: dict, unit: Optional[Unit] = None) -> bool:
        if "all" in cond:
            return all(self.evaluate(c, unit) for c in cond["all"])
        if "any" in cond:
            return any(self.evaluate(c, unit) for c in cond["any"])
        if "not" in cond:
            return not self.evaluate(cond["not"], unit)
        op = next(k for k in cond if k in COND_OPS)
        present, value = False, None
        if "input" in cond:
            present = cond["input"] in self.state["inputs"]
            value = self.state["inputs"].get(cond["input"])
        else:
            path = self.artifacts_dir / self._art(cond["file"], unit)
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                if cond.get("field"):
                    try:
                        present, value = _dig(json.loads(text), cond["field"])
                    except json.JSONDecodeError:
                        present = False
                else:
                    present, value = True, text
        if op == "exists":
            return present == cond["exists"]
        return present and _compare(op, value, cond[op])  # a missing value fails every other test

    def _sim_take(self, key: str, unit_id: str) -> bool:
        """In a simulation, consume one forced outcome for this step."""
        if self.sim is None:
            return False
        counts = self.sim.get(key, {})
        if counts.get(unit_id, 0) > 0:
            counts[unit_id] -= 1
            return True
        return False

    def _when_holds(self, cond: dict, unit_id: str, unit: Optional[Unit]) -> bool:
        if self.sim is not None and unit_id in self.sim.get("skip", set()):
            return False
        return self.evaluate(cond, unit)

    # ---- deciding what's next --------------------------------------------- #

    def next_action(self) -> dict:
        for sdef in self.flow["steps"]:
            st = self.state["steps"][sdef["id"]]
            if st["status"] in DONE:
                continue
            if sdef.get("kind") == "foreach":
                act = self._foreach_action(sdef, st)
            else:
                act = self._unit_action(Unit(sdef["id"], sdef, sdef, st, None))
            if act is not None:
                return act
        return {
            "action": "done",
            "run": self.state["run_id"],
            "run_dir": str(self.run_dir),
            "artifacts": list(dict.fromkeys(self._abs(p) for sid in self.steps for p in self._step_artifacts(sid))),
        }

    def _foreach_action(self, sdef: dict, st: dict) -> Optional[dict]:
        if st["items"] is None:
            if "when" in sdef and not self._when_holds(sdef["when"], sdef["id"], None):
                st.update(status="skipped", items=[])
                self._event("skip", step=sdef["id"], condition=describe_condition(sdef["when"]))
                return None
            self._expand(sdef, st)
        respect_deps = sdef.get("respect_item_dependencies", True)
        while True:
            items = st["items"]
            if all(it["status"] in DONE for it in items):
                st["status"] = "done"
                self._event("step_done", step=sdef["id"])
                return None
            if st["status"] == "pending":
                st["status"] = "running"
            done_ids = {it["id"] for it in items if it["status"] in DONE}
            progressed = False
            for it in items:
                if it["status"] in DONE or (respect_deps and not set(it["depends_on"]) <= done_ids):
                    continue
                for sub in sdef["cycle"]:
                    sst = it["steps"][sub["id"]]
                    if sst["status"] in DONE:
                        continue
                    act = self._unit_action(Unit(f"{sdef['id']}[{it['id']}].{sub['id']}", sdef, sub, sst, it))
                    if act is not None:
                        return act
                    progressed = True  # the cycle step was skipped; look again
                    break
                else:
                    _refresh_item_state(it)
                    progressed = True
                if progressed:
                    break
            if not progressed:
                raise EngineError(f"'{sdef['id']}': no item is runnable; item dependencies are inconsistent")

    def _expand(self, sdef: dict, st: dict) -> None:
        over = self.steps[sdef["over"]]
        pi = over["produces_items"]
        items = check_items(self.flow, pi, read_json(self.artifacts_dir / pi["file"], pi["file"]))
        id_field = pi.get("id_field", "id")
        dep_field = pi.get("depends_on_field", "depends_on")
        st["items"] = []
        for item in items:
            rel = f"{sdef['id']}/{item[id_field]}/item.json"
            atomic_write(self.artifacts_dir / rel, json.dumps(item, indent=2) + "\n")
            st["items"].append({
                "id": item[id_field],
                "title": item.get("title"),
                "depends_on": list(item.get(dep_field, [])),
                "status": "pending",
                "item_file": rel,
                "steps": {sub["id"]: fresh_unit(sub) for sub in sdef["cycle"]},
            })
        self._event("expand", step=sdef["id"], items=[it["id"] for it in st["items"]])

    def _unit_action(self, unit: Unit) -> Optional[dict]:
        """The action for the current unit, or None if it just got skipped or finished."""
        st, ud = unit.st, unit.unit_def
        if st["status"] == "failed":
            return self._failed_action(unit)
        if st["status"] == "awaiting_gate":
            return self._gate_action(unit)
        if st["status"] == "pending":
            if "when" in ud and not self._when_holds(ud["when"], ud["id"], unit):
                st.update(status="skipped", completed_at=now(), feedback=None, feedback_files=[])
                self._refresh_item(unit)
                self._event("skip", step=unit.path, condition=describe_condition(ud["when"]))
                return None
            gate = st.get("gate")
            if gate and gate["when"] == "before" and gate["status"] != "approved":
                return self._gate_action(unit)
            if not wait_kind(ud):
                return self._run_action(unit)
            st.update(status="waiting", started_at=now(), attempt=max(st["attempt"], 1),
                      wait={"started_at": now(), "next_check_at": now(), "polls": 0, "last_status": None,
                            "last_checked_at": None})
            self._refresh_item(unit)
            self._event("wait_start", step=unit.path)
        if st["status"] == "waiting":
            return self._wait_action(unit)
        return self._run_action(unit)  # running: resume it

    def _failed_action(self, unit: Unit) -> dict:
        return {
            "action": "failed",
            "step": unit.path,
            "reason": unit.st["error"],
            "feedback": unit.st["feedback"],
            "hint": "Show the reason to the user. `retry` resets the attempt counter and runs the step again; feedback is kept.",
            "commands": {"retry": f"{self.cli} retry {shlex.quote(unit.path)}"},
        }

    def _run_action(self, unit: Unit) -> dict:
        ud, st = unit.unit_def, unit.st
        defaults = self.flow.get("defaults", {})
        running = st["status"] == "running"
        act: dict[str, Any] = {
            "action": "run_step",
            "step": unit.path,
            "skill": str(self.flow_path.parent / ud["skill"]),
            "run_in_subagent": bool(ud.get("agents")) or ud.get(
                "run_in_subagent", unit.step_def.get("run_in_subagent", defaults.get("run_in_subagent", True))),
            "subagent_type": self._subagent_type(ud, unit.step_def),
            "attempt": st["attempt"] if running or st["status"] == "waiting" else st["attempt"] + 1,
            "resumed": running,
            "inputs": self._inputs(unit),
            "outputs": [self._abs(self._art(a, unit)) for a in unit_artifacts(ud)],
            "contract": describe_contract(ud),
            "feedback": st["feedback"],
            "run": self.state["run_id"],
            "run_inputs": self.state["inputs"],
            "artifacts_dir": str(self.artifacts_dir),
        }
        if unit.item:
            act["item"] = read_json(self.artifacts_dir / unit.item["item_file"], "item file")
        pi = ud.get("produces_items")
        if pi:
            act["items_file"] = self._abs(pi["file"])
            if pi.get("item_schema"):
                act["item_schema"] = flow_schemas(self.flow)[pi["item_schema"]]
        if wait_kind(ud) == "agent":
            status_path = self._abs(self._status_rel(unit))
            act["outputs"].append(status_path)
            act["wait_check"] = {
                "status_file": status_path,
                "format": '{"status": "ready" | "pending" | "failed", "summary": "<one line>"}',
                "polls_so_far": (st["wait"] or {}).get("polls", 0),
            }
            act["contract"].append(f"`{status_path}`: JSON with `status` (ready, pending or failed) and a `summary`")
        q = shlex.quote(unit.path)
        act["commands"] = {"start": f"{self.cli} start {q}"}
        if ud.get("agents"):
            # One subagent per entry, all launched at once. Agents already done
            # (e.g. before an interruption or a failed sibling) are not listed.
            act["agents"] = [
                {
                    "agent": ad["id"],
                    "skill": str(self.flow_path.parent / ad.get("skill", ud["skill"])),
                    "focus": ad.get("focus"),
                    "subagent_type": self._subagent_type(ad, ud, unit.step_def),
                    "outputs": [self._abs(self._art(a, unit)) for a in agent_artifacts(ud, ad)],
                    "contract": describe_contract(ud, ad),
                    "commands": {
                        "complete": f"{self.cli} complete {q} --agent {ad['id']} --summary '<one line>'",
                        "fail": f"{self.cli} fail {q} --agent {ad['id']} --reason '<why it cannot be done>'",
                    },
                }
                for ad in ud["agents"] if st["agents"][ad["id"]]["status"] != "done"
            ]
        else:
            act["commands"]["complete"] = f"{self.cli} complete {q} --summary '<one line>'"
            act["commands"]["fail"] = f"{self.cli} fail {q} --reason '<why it cannot be done>'"
        return act

    def _gate_action(self, unit: Unit) -> dict:
        gdef, gate = unit.unit_def["gate"], unit.st["gate"]
        before = gate["when"] == "before"
        default_prompt = (
            f"Approve before '{unit.path}' runs." if before
            else f"Review the output of '{unit.path}', then approve it or reject it with feedback."
        )
        return {
            "action": "await_gate",
            "gate": gate["id"],
            "step": unit.path,
            "when": gate["when"],
            "prompt": gdef.get("prompt", default_prompt),
            "review": self._inputs(unit) if before else [self._abs(p) for p in unit.st["artifacts"]],
            "summary": unit.st["summary"],
            "attempt": unit.st["attempt"],
            "commands": {
                "approve": f"{self.cli} approve {shlex.quote(unit.path)} --note '<optional>'",
                "reject": f"{self.cli} reject {shlex.quote(unit.path)} --feedback '<what must change>'",
            },
        }

    # ---- waiting on something external ------------------------------------ #

    def _wait_action(self, unit: Unit) -> Optional[dict]:
        wd, w = unit.unit_def["wait"], unit.st["wait"]
        current = _clock()
        timeout = wd.get("timeout")
        if timeout is not None and current >= _ts(w["started_at"]) + timedelta(seconds=parse_duration(timeout)):
            self._wait_timed_out(unit)
            return self._unit_action(unit) if unit.st["status"] not in DONE else None
        due = current >= _ts(w["next_check_at"])
        if due and wait_kind(unit.unit_def) == "agent":
            return self._run_action(unit)
        q = shlex.quote(unit.path)
        seconds = max(0, int((_ts(w["next_check_at"]) - current).total_seconds()))
        act = {
            "action": "wait",
            "step": unit.path,
            "description": wd.get("description") or f"waiting for '{unit.unit_def['id']}'",
            "check": wait_kind(unit.unit_def) if wait_kind(unit.unit_def) == "agent" else wd["check"]["run"],
            "due": due,
            "next_check_at": w["next_check_at"],
            "seconds_until_due": seconds,
            "polls": w["polls"],
            "last_status": w["last_status"],
            "status_file": self._abs(self._status_rel(unit)),
            "timeout_at": (_ts(w["started_at"]) + timedelta(seconds=parse_duration(timeout))).isoformat(timespec="seconds")
            if timeout is not None else None,
            "commands": {
                "poll": f"{self.cli} poll {q}",
                "poll_blocking": f"{self.cli} poll {q} --block {MAX_BLOCK_SECONDS}",
            },
            "hint": ("Run commands.poll now: the check is due." if due else
                     f"Nothing to do for {seconds}s. If that is under ~10 minutes run commands.poll_blocking "
                     "(with a 10-minute shell timeout); "
                     "otherwise stop and tell the user the run is waiting and when to resume it."),
        }
        return act

    def _wait_timed_out(self, unit: Unit) -> None:
        wd = unit.unit_def["wait"]
        message = f"timed out after {wd['timeout']} waiting ({unit.st['wait']['polls']} checks, last: {unit.st['wait']['last_status']})"
        self._event("wait_timeout", step=unit.path)
        if wd.get("on_timeout", "fail") == "continue":
            self._finish(unit, [], f"continued after it {message}")
        else:
            unit.st.update(status="failed", error=f"'{unit.unit_def['id']}' {message}")
            self._refresh_item(unit)

    def poll(self, path: str, block: int = 0, force: bool = False) -> dict:
        """Check a waiting step now (if due, or always with force). With block,
        keep waiting up to that many seconds for it to stop being pending."""
        self.next_action()  # move the current step into its waiting state if needed
        unit = self.resolve(path)
        if unit.st["status"] != "waiting":
            raise EngineError(f"'{unit.path}' is {unit.st['status']}, not waiting")
        deadline = _clock() + timedelta(seconds=max(0, min(block, MAX_BLOCK_SECONDS)))
        outcome: dict = {}
        while True:
            act = self._wait_action(unit)
            if unit.st["status"] != "waiting":
                break  # timed out
            w = unit.st["wait"]
            if force or act is None or act["action"] != "wait" or act["due"]:
                force = False
                if wait_kind(unit.unit_def) == "agent":
                    w["next_check_at"] = now()
                    break  # due: the next action is the subagent check
                status, output = self._run_wait_check(unit)
                outcome = self._apply_wait_status(unit, status, output)
                outcome["last_status"] = status
                if status != "pending":
                    break
                continue
            remaining = (deadline - _clock()).total_seconds()
            if remaining <= 0:
                break
            _sleep(min(remaining, max(1, act["seconds_until_due"])))
        w = unit.st.get("wait") or {}
        result = {"ok": True, "step": unit.path, "status": outcome.get("status", unit.st["status"]),
                  "last_status": outcome.get("last_status", w.get("last_status")), "next_check_at": w.get("next_check_at")}
        if "sent_back_to" in outcome:
            result["sent_back_to"] = outcome["sent_back_to"]
        result["next"] = self.next_action()
        return result

    def _expand_command(self, cmd: str, unit: Optional[Unit]) -> str:
        values = {"artifacts_dir": str(self.artifacts_dir), "run_dir": str(self.run_dir),
                  "run": self.state["run_id"], "item": unit.item["id"] if unit and unit.item else ""}
        values.update({f"input.{k}": str(v) for k, v in self.state["inputs"].items()})
        return re.sub(r"\{([a-z_]+(?:\.[A-Za-z0-9_]+)?)\}",
                      lambda m: shlex.quote(values[m[1]]) if m[1] in values else m[0], cmd)

    def _shell(self, cmd: str, unit: Optional[Unit], timeout: int) -> tuple[Optional[int], str]:
        try:
            proc = subprocess.run(self._expand_command(cmd, unit), shell=True, cwd=self.project_dir,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return None, _tail(out + f"\n(timed out after {timeout}s)")
        return proc.returncode, _tail((proc.stdout or "") + (proc.stderr or ""))

    def _run_wait_check(self, unit: Unit) -> tuple[str, str]:
        uid = unit.unit_def["id"]
        if self.sim is not None:
            if self._sim_take("wait_fail", uid):
                return "failed", "simulated failure"
            if self._sim_take("wait", uid):
                return "pending", "simulated: not ready yet"
            return "ready", "simulated: ready"
        check = unit.unit_def["wait"]["check"]
        code, output = self._shell(check["run"], unit, parse_duration(check.get("timeout", 300)))
        if code is None:
            return "pending", output
        if code in check.get("ready_exit_codes", [0]):
            return "ready", output
        if code in check.get("failed_exit_codes", []):
            return "failed", f"exit code {code}\n{output}"
        return "pending", output  # anything else: not yet; the timeout bounds the wait

    def _apply_wait_status(self, unit: Unit, status: str, output: str, arts: Optional[list[str]] = None) -> dict:
        st, wd = unit.st, unit.unit_def["wait"]
        w = st["wait"]
        rel = self._status_rel(unit)
        w.update(polls=w["polls"] + 1, last_status=status, last_checked_at=now())
        if wait_kind(unit.unit_def) == "command":
            atomic_write(self.artifacts_dir / rel, json.dumps(
                {"status": status, "checked_at": w["last_checked_at"], "polls": w["polls"], "output": output},
                indent=2) + "\n")
        self._event("poll", step=unit.path, status=status)
        first_line = (output or "").strip().splitlines()[0] if (output or "").strip() else status
        if status == "pending":
            interval = parse_duration(wd.get("poll_every", DEFAULT_POLL_EVERY))
            w["next_check_at"] = (_clock() + timedelta(seconds=interval)).isoformat(timespec="seconds")
            st["status"] = "waiting"
            self._refresh_item(unit)
            return {"ok": True, "step": unit.path, "status": "waiting", "next": self.next_action()}
        if status == "ready":
            return self._finish(unit, list(dict.fromkeys((arts or []) + [rel])),
                                f"ready after {w['polls']} check(s): {first_line}")
        on_failure = wd.get("on_failure", "fail")
        if on_failure == "fail":
            st.update(status="failed", error=f"external check '{unit.unit_def['id']}' failed: {first_line} (see {self._abs(rel)})",
                      feedback_files=[rel])
            self._refresh_item(unit)
            self._event("fail", step=unit.path, reason="external check failed")
            return {"ok": True, "step": unit.path, "status": "failed", "next": self.next_action()}
        message = f"External check '{unit.unit_def['id']}' failed: {first_line}. Details: {self._abs(rel)}"
        sent = self._send_back_any(unit, on_failure["back_to"], message, [rel], limit="default")
        self._event("wait_failed", step=unit.path, sent_back_to=sent)
        return {"ok": True, "step": unit.path, "status": "failed_externally", "sent_back_to": sent, "next": self.next_action()}

    # ---- transitions -------------------------------------------------------- #

    def start(self, path: str) -> dict:
        unit = self.resolve(path)
        st = unit.st
        if st["status"] != "running":
            act = self.next_action()
            if act["action"] != "run_step" or act["step"] != unit.path:
                raise EngineError(f"'{unit.path}' cannot start now; the next action is {_brief(act)}")
            new_attempt = st["attempt"] if st["status"] == "waiting" else st["attempt"] + 1
            st.update(status="running", attempt=new_attempt, started_at=now(), completed_at=None, error=None,
                      fix_failures=0)
            for ag in st.get("agents", {}).values():
                if ag["status"] != "done":
                    ag.update(status="running", started_at=now(), completed_at=None, error=None, fix_failures=0)
            self._refresh_item(unit)
            self._event("start", step=unit.path, attempt=st["attempt"])
        res = {"ok": True, "step": unit.path, "status": "running", "attempt": st["attempt"]}
        if "agents" in st:
            res["agents"] = [aid for aid, ag in st["agents"].items() if ag["status"] == "running"]
        return res

    def complete(self, path: str, extra: tuple = (), summary: Optional[str] = None, agent: Optional[str] = None) -> dict:
        """Record finished work. The output must pass its contract (and the
        step's checks); otherwise VerificationError explains what to fix."""
        unit = self.resolve(path)
        st, ud = unit.st, unit.unit_def
        if st["status"] != "running":
            raise EngineError(f"'{unit.path}' is {st['status']}, not running; call `start {unit.path}` first")
        ad, ag = self._agent(unit, agent)
        specs = artifact_specs(ud, ad) if ad else artifact_specs(ud)
        if wait_kind(ud) == "agent":
            specs = specs + [{"path": wait_status_path(ud, unit.item is not None), "json_schema": WAIT_STATUS_SCHEMA}]
        arts, problems = self._collect(unit, specs, extra)
        problems += self._verify(unit, specs)
        if not problems and ag is None:
            pi = ud.get("produces_items")
            if pi:
                try:
                    check_items(self.flow, pi, read_json(self.artifacts_dir / pi["file"], pi["file"]))
                except EngineError as e:
                    problems.append(str(e))
            if not problems:
                problems += self._run_checks(unit)
        if problems:
            self._refuse(unit, ad, ag, problems)
        if ag is not None:
            ag.update(status="done", artifacts=arts, summary=summary, completed_at=now())
            self._event("complete", step=unit.path, agent=agent, artifacts=arts, summary=summary)
            return self._settle_agents(unit)
        if wait_kind(ud) == "agent":
            data = read_json(self.artifacts_dir / self._status_rel(unit), "status file")
            st["summary"] = summary
            return self._apply_wait_status(unit, data["status"], data.get("summary") or summary or "",
                                           [a for a in arts if a != self._status_rel(unit)])
        return self._finish(unit, arts, summary)

    def _agent(self, unit: Unit, agent: Optional[str]) -> tuple[Optional[dict], Optional[dict]]:
        """Resolve --agent: required for steps with parallel agents, refused otherwise."""
        agents = unit.unit_def.get("agents")
        if not agents:
            if agent:
                raise EngineError(f"'{unit.path}' has no parallel agents; drop --agent")
            return None, None
        ids = [a["id"] for a in agents]
        if not agent:
            raise EngineError(f"'{unit.path}' runs parallel agents ({', '.join(ids)}); report each with --agent <id>")
        if agent not in ids:
            raise EngineError(f"'{unit.path}' has no agent '{agent}'; agents are: {', '.join(ids)}")
        ag = unit.st["agents"][agent]
        if ag["status"] != "running":
            raise EngineError(f"agent '{agent}' of '{unit.path}' is {ag['status']}, not running")
        return next(a for a in agents if a["id"] == agent), ag

    def _collect(self, unit: Unit, specs: list[dict], extra: tuple) -> tuple[list[str], list[str]]:
        arts: list[str] = []
        problems: list[str] = []
        for spec in specs:
            rel = self._art(spec["path"], unit)
            if (self.artifacts_dir / rel).is_file():
                arts.append(rel)
        for p in extra:
            pp = Path(p)
            if not pp.is_absolute():
                pp = self.artifacts_dir / pp if (self.artifacts_dir / pp).exists() else Path.cwd() / pp
            pp = pp.resolve()
            if not pp.exists():
                problems.append(f"{p}: extra artifact not found")
                continue
            try:
                arts.append(str(pp.relative_to(self.artifacts_dir)))
            except ValueError:
                arts.append(str(pp))
        return list(dict.fromkeys(arts)), problems

    def _verify(self, unit: Unit, specs: list[dict]) -> list[str]:
        problems = []
        schemas = flow_schemas(self.flow)
        for spec in specs:
            rel = self._art(spec["path"], unit)
            path = self.artifacts_dir / rel
            if not path.is_file():
                if not spec.get("optional"):
                    problems.append(f"{path}: missing; write it")
                continue
            if self.sim is not None:
                continue  # simulations check existence only
            data = path.read_bytes()
            text = data.decode("utf-8", errors="replace")
            if spec.get("min_bytes") and len(data) < spec["min_bytes"]:
                problems.append(f"{rel}: only {len(data)} bytes, needs at least {spec['min_bytes']}")
            for heading in spec.get("sections", []):
                if not re.search(r"(?m)^[ \t]*" + re.escape(heading) + r"(?=[\s:]|$)", text):
                    problems.append(f"{rel}: missing the heading `{heading}` (a line starting with it)")
            for pattern in spec.get("contains", []):
                if not re.search(pattern, text, re.M):
                    problems.append(f"{rel}: does not match /{pattern}/")
            js = spec.get("json_schema")
            if js is not None:
                schema = js if isinstance(js, dict) else schemas[js]
                try:
                    problems += [f"{rel}: {e}" for e in schema_errors(json.loads(text), schema)]
                except json.JSONDecodeError as e:
                    problems.append(f"{rel}: not valid JSON ({e})")
        return problems

    def _run_checks(self, unit: Unit) -> list[str]:
        problems = []
        for c in unit.unit_def.get("checks", []):
            cmd = check_command(c)
            if self.sim is not None:
                continue
            timeout = parse_duration(c.get("timeout", DEFAULT_CHECK_TIMEOUT)) if isinstance(c, dict) else DEFAULT_CHECK_TIMEOUT
            code, output = self._shell(cmd, unit, timeout)
            if code != 0:
                name = c.get("name", cmd) if isinstance(c, dict) else cmd
                status = "timed out" if code is None else f"exit {code}"
                problems.append(f"check `{name}` failed ({status}): {cmd}" + (f"\n{output}" if output else ""))
        return problems

    def _refuse(self, unit: Unit, ad: Optional[dict], ag: Optional[dict], problems: list[str]) -> None:
        counter = ag if ag is not None else unit.st
        counter["fix_failures"] = counter.get("fix_failures", 0) + 1
        limit = self._setting("max_fix_attempts", DEFAULT_MAX_FIX_ATTEMPTS, unit.unit_def, unit.step_def)
        message = "output did not pass verification:\n- " + "\n- ".join(problems)
        who = f"agent '{ad['id']}' of '{unit.path}'" if ad else f"'{unit.path}'"
        self._event("refused", step=unit.path, agent=ad and ad["id"], problems=problems)
        if counter["fix_failures"] >= limit:
            reason = f"output failed verification {limit} times; last problems: " + "; ".join(problems)[:1500]
            if ag is not None:
                ag.update(status="failed", error=reason, completed_at=now())
                self._settle_agents(unit)
            else:
                unit.st.update(status="failed", error=reason)
                self._refresh_item(unit)
            raise VerificationError(f"{message}\nThat was try {limit} of {limit}: {who} is now failed and the user decides.")
        left = limit - counter["fix_failures"]
        raise VerificationError(f"{message}\nFix these (have the subagent revise its output), then complete again. "
                                f"{left} more refusal(s) allowed before {who} fails.")

    def _settle_agents(self, unit: Unit) -> dict:
        """After one parallel agent reports: wait for the rest, then finish or fail the step."""
        agents = unit.st["agents"]
        running = [aid for aid, ag in agents.items() if ag["status"] == "running"]
        if running:
            return {"ok": True, "step": unit.path, "status": "running", "waiting_for": running}
        failed = {aid: ag["error"] for aid, ag in agents.items() if ag["status"] == "failed"}
        if failed:
            unit.st.update(status="failed", error="; ".join(f"agent '{a}' failed: {e}" for a, e in failed.items()))
            self._refresh_item(unit)
            self._event("fail", step=unit.path, agents=list(failed))
            return {"ok": True, "step": unit.path, "status": "failed", "next": self.next_action()}
        arts = [p for ag in agents.values() for p in ag["artifacts"]]
        summary = " | ".join(f"{aid}: {ag['summary']}" for aid, ag in agents.items() if ag["summary"]) or None
        return self._finish(unit, list(dict.fromkeys(arts)), summary)

    def _finish(self, unit: Unit, arts: list[str], summary: Optional[str]) -> dict:
        """Work accepted: loop back if `repeat` says so, else go to the gate or finish."""
        st, ud = unit.st, unit.unit_def
        st.update(artifacts=arts, summary=summary, completed_at=now(), fix_failures=0)
        self._event("complete", step=unit.path, artifacts=arts, summary=summary)
        rep = ud.get("repeat")
        if rep:
            met = not self._sim_take("until", ud["id"]) and self.evaluate(rep["until"], unit)
            if not met:
                rounds = rep.get("max", self._max_iterations(ud, unit.step_def))
                condition = describe_condition(rep["until"])
                if st["loops"] + 1 < rounds:
                    st["loops"] += 1
                    target = rep.get("back_to", ud["id"])
                    message = (f"Automatic check not met after round {st['loops']} of {rounds}: {condition}. "
                               "Read the files listed with this feedback and fix what they report.")
                    files = list(arts) + [self._art(p, unit) for p in rep.get("feedback", [])]
                    sent = self._send_back_any(unit, target, message, files, limit=None)
                    self._event("repeat", step=unit.path, round=st["loops"], sent_back_to=sent, condition=condition)
                    return {"ok": True, "step": unit.path, "status": "repeating", "round": st["loops"],
                            "sent_back_to": sent, "next": self.next_action()}
                note = f"condition still not met after {rounds} rounds: {condition}"
                on = rep.get("on_exhausted", "fail")
                self._event("repeat_exhausted", step=unit.path, on_exhausted=on)
                if on == "fail":
                    st.update(status="failed", error=note)
                    self._refresh_item(unit)
                    return {"ok": True, "step": unit.path, "status": "failed", "next": self.next_action()}
                st["summary"] = f"{summary + ' ' if summary else ''}({note})"
        gate = st.get("gate")
        if gate and gate["when"] == "after":
            st["status"] = "awaiting_gate"
            gate["status"] = "pending"
        else:
            st.update(status="done", feedback=None, feedback_files=[])
        self._refresh_item(unit)
        return {"ok": True, "step": unit.path, "status": st["status"], "next": self.next_action()}

    def fail(self, path: str, reason: str, agent: Optional[str] = None) -> dict:
        unit = self.resolve(path)
        if agent is not None or (unit.unit_def.get("agents") and unit.st["status"] == "running"):
            _, ag = self._agent(unit, agent)
            ag.update(status="failed", error=reason, completed_at=now())
            self._event("fail", step=unit.path, agent=agent, reason=reason)
            return self._settle_agents(unit)
        if unit.st["status"] not in ("running", "waiting"):
            act = self.next_action()
            if act.get("step") != unit.path or unit.st["status"] != "pending":
                raise EngineError(f"'{unit.path}' is {unit.st['status']}; only the current or a running step can fail")
        unit.st.update(status="failed", error=reason)
        self._refresh_item(unit)
        self._event("fail", step=unit.path, reason=reason)
        return {"ok": True, "step": unit.path, "status": "failed", "next": self.next_action()}

    def approve(self, target: str, note: Optional[str] = None) -> dict:
        unit = self._waiting_gate(target)
        gate = unit.st["gate"]
        gate["status"] = "approved"
        gate["decisions"].append({"at": now(), "decision": "approved", "note": note, "attempt": unit.st["attempt"]})
        if gate["when"] == "after":
            unit.st.update(status="done", feedback=None, feedback_files=[])
        self._refresh_item(unit)
        self._event("approve", gate=gate["id"], step=unit.path, note=note)
        return {"ok": True, "gate": gate["id"], "step": unit.path, "next": self.next_action()}

    def reject(self, target: str, feedback: str) -> dict:
        if not feedback or not feedback.strip():
            raise EngineError("reject needs --feedback saying what must change")
        unit = self._waiting_gate(target)
        gate, gdef = unit.st["gate"], unit.unit_def["gate"]
        gate["status"] = "rejected"
        gate["decisions"].append({"at": now(), "decision": "rejected", "feedback": feedback, "attempt": unit.st["attempt"]})
        unit.st["loops"] = 0  # a human decision gives automatic loops a fresh budget
        message = f"Gate {gate['id']} rejected: {feedback}"
        on_reject = gdef.get("on_reject")
        if gate["when"] == "before" and not on_reject:
            unit.st.update(status="failed", error=f"{message} (the gate is before the step, so nothing ran)", feedback=message)
            unit.st["feedback_history"].append({"at": now(), "feedback": message})
            sent_back = unit.path
        else:
            files = list(unit.st["artifacts"]) if gate["when"] == "after" else []
            sent_back = self._send_back_any(unit, on_reject or unit.unit_def["id"], message, files, limit="default")
        self._refresh_item(unit)
        self._event("reject", gate=gate["id"], step=unit.path, feedback=feedback, sent_back_to=sent_back)
        return {"ok": True, "gate": gate["id"], "step": unit.path, "sent_back_to": sent_back, "next": self.next_action()}

    def retry(self, path: str) -> dict:
        unit = self.resolve(path)
        if unit.st["status"] != "failed":
            raise EngineError(f"'{unit.path}' is {unit.st['status']}; only a failed step can be retried")
        unit.st.update(status="pending", attempt=0, error=None, loops=0, fix_failures=0, wait=None)
        if unit.st.get("gate") and unit.st["gate"]["status"] == "rejected":
            unit.st["gate"]["status"] = "pending"
        for ag in unit.st.get("agents", {}).values():  # agents that finished keep their work
            if ag["status"] != "done":
                ag.update(fresh_agent())
        self._refresh_item(unit)
        self._event("retry", step=unit.path)
        return {"ok": True, "step": unit.path, "next": self.next_action()}

    def _waiting_gate(self, target: str) -> Unit:
        act = self.next_action()
        if act["action"] != "await_gate":
            raise EngineError(f"no gate is waiting; the next action is {_brief(act)}")
        if target not in (act["gate"], act["step"]):
            raise EngineError(f"the waiting gate is {act['gate']} on '{act['step']}', not '{target}'")
        return self.resolve(act["step"])

    def _send_back_any(self, unit: Unit, target_id: str, message: str, files: list[str], limit: Any) -> str:
        """Send work back to target_id with feedback. limit: "default" uses the
        target's max_iterations, None never fails the target."""
        if unit.item:
            cycle = unit.step_def["cycle"]
            idx = next(i for i, s in enumerate(cycle) if s["id"] == target_id)
            for sub in cycle[idx:]:
                _reset_unit(unit.item["steps"][sub["id"]])
            target_st, tdef = unit.item["steps"][target_id], cycle[idx]
            sent = f"{unit.step_def['id']}[{unit.item['id']}].{target_id}"
        else:
            for sid in {target_id, unit.step_def["id"]} | self._dependents(target_id):
                sdef = self.steps[sid]
                if sdef.get("kind") == "foreach":
                    self.state["steps"][sid] = fresh_step_state(sdef)  # items are re-read on the next pass
                else:
                    _reset_unit(self.state["steps"][sid])
            target_st, tdef = self.state["steps"][target_id], self.steps[target_id]
            sent = target_id
        context = unit.step_def if unit.item else tdef
        max_it = self._max_iterations(tdef, context) if limit == "default" else limit
        _give_feedback(target_st, message, max_it, files)
        self._refresh_item(unit)
        return sent

    def _dependents(self, sid: str) -> set[str]:
        found = {sid}
        for sdef in self.flow["steps"]:
            refs = set(sdef.get("needs", [])) | set(sdef.get("inputs", [])) | ({sdef["over"]} if "over" in sdef else set())
            if refs & found:
                found.add(sdef["id"])
        return found - {sid}

    @staticmethod
    def _refresh_item(unit: Unit) -> None:
        if unit.item:
            _refresh_item_state(unit.item)

    # ---- reporting ---------------------------------------------------------- #

    def render_status(self) -> str:
        marks = {"done": "x", "skipped": "-", "running": ">", "waiting": "~", "awaiting_gate": "?",
                 "failed": "!", "pending": " "}
        act = self.next_action()
        lines = [f"{self.flow['name']} · run {self.state['run_id']} · next: {_brief(act)}", ""]

        def unit_line(indent: str, label: str, st: dict) -> str:
            info = st["status"]
            if st.get("gate"):
                g = st["gate"]
                info += f" · gate {g['id']} ({g['when']}) {g['status']}"
            if st["attempt"] > 1:
                info += f" · attempt {st['attempt']}"
            if st.get("loops"):
                info += f" · {st['loops']} automatic loop(s)"
            if st["status"] == "waiting" and st.get("wait"):
                w = st["wait"]
                info += f" · {w['polls']} check(s), last {w['last_status'] or '-'}, next at {w['next_check_at']}"
            if st["feedback"] and st["status"] not in ("failed",) + DONE:
                info += " · has feedback"
            if st["status"] == "failed" and st["error"]:
                info += f" · {st['error']}"
            if st.get("agents"):
                info += " · agents: " + ", ".join(f"{aid} {ag['status']}" for aid, ag in st["agents"].items())
            return f"{indent}[{marks[st['status']]}] {label:<24} {info}"

        for sdef in self.flow["steps"]:
            st = self.state["steps"][sdef["id"]]
            if sdef.get("kind") != "foreach":
                lines.append(unit_line("", sdef["id"], st))
                continue
            items = st["items"]
            count = f"{sum(i['status'] in DONE for i in items)}/{len(items)} items done" if items is not None else "not expanded yet"
            lines.append(f"[{marks.get(st['status'], ' ')}] {sdef['id']:<24} foreach over {sdef['over']} · {count}")
            for it in items or []:
                deps = f" (after {', '.join(it['depends_on'])})" if it["depends_on"] else ""
                lines.append(f"    {it['id']}: {it.get('title') or ''}{deps}")
                for sub in sdef["cycle"]:
                    lines.append(unit_line("      ", sub["id"], it["steps"][sub["id"]]))
        return "\n".join(lines)


def _refresh_item_state(item: dict) -> None:
    statuses = [s["status"] for s in item["steps"].values()]
    if all(s in DONE for s in statuses):
        item["status"] = "done"
    elif "failed" in statuses:
        item["status"] = "failed"
    elif all(s == "pending" for s in statuses):
        item["status"] = "pending"
    else:
        item["status"] = "running"


def _give_feedback(st: dict, message: str, max_iterations: Optional[int], files: list[str]) -> None:
    st["feedback"] = message
    st["feedback_files"] = list(dict.fromkeys(files))
    st["feedback_history"].append({"at": now(), "feedback": message})
    if max_iterations is not None and st["attempt"] >= max_iterations:
        st["status"] = "failed"
        st["error"] = f"still not accepted after {st['attempt']} attempt(s) (max_iterations={max_iterations}). {message}"


def _reset_unit(st: dict) -> None:
    if st["artifacts"]:
        st["previous_artifacts"] = st["artifacts"]
    st.update(status="pending", artifacts=[], summary=None, error=None, started_at=None, completed_at=None,
              fix_failures=0, wait=None, feedback_files=[])
    if st.get("gate"):
        st["gate"]["status"] = "pending"
    for aid in st.get("agents", {}):
        st["agents"][aid] = fresh_agent()


def _brief(act: dict) -> str:
    if act["action"] == "run_step":
        return f"run_step {act['step']}"
    if act["action"] == "await_gate":
        return f"await_gate {act['gate']} on {act['step']}"
    if act["action"] == "failed":
        return f"failed {act['step']}"
    if act["action"] == "wait":
        return f"wait {act['step']} ({'due now' if act['due'] else 'next check at ' + act['next_check_at']})"
    return act["action"]


# --------------------------------------------------------------------------- #
# Simulation, guard hook, run listing
# --------------------------------------------------------------------------- #

def _fake_value(schema: dict) -> Any:
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type", "string")
    t = t[0] if isinstance(t, list) else t
    if t == "object":
        props = schema.get("properties", {})
        return {k: _fake_value(props.get(k, {})) for k in schema.get("required", list(props))}
    if t == "array":
        return [_fake_value(schema.get("items", {})) for _ in range(max(1, schema.get("minItems", 1)))]
    if t in ("integer", "number"):
        return max(1, schema.get("minimum", 1))
    return {"boolean": True, "null": None}.get(t, "simulated")


def fake_items(flow: dict, pi: dict, count: int = 2) -> Any:
    schema = flow_schemas(flow).get(pi.get("item_schema"), {})
    id_field = pi.get("id_field", "id")
    dep_field = pi.get("depends_on_field", "depends_on")
    items = []
    for n in range(1, count + 1):
        item = _fake_value({"type": "object", **schema}) if schema else {}
        item[id_field] = f"T{n}"
        item[dep_field] = [f"T{n - 1}"] if n > 1 else []
        items.append(item)
    return {pi["key"]: items} if pi.get("key") else items


def _fake_output(run: Run, unit: Unit, spec: dict) -> str:
    js = spec.get("json_schema")
    if js is not None:
        return json.dumps(_fake_value(js if isinstance(js, dict) else flow_schemas(run.flow)[js]), indent=2)
    return "".join(f"{h}\nsimulated\n\n" for h in spec.get("sections", [])) or "simulated\n"


def simulate(flow_path: Path, rejects: Optional[dict[str, int]] = None, until: Optional[dict[str, int]] = None,
             skip: Optional[set[str]] = None, waits: Optional[dict[str, int]] = None,
             wait_fails: Optional[dict[str, int]] = None, max_actions: int = 500) -> list[str]:
    """Walk a flow with fake artifacts, approving every gate and accepting every
    repeat and wait, except the outcomes forced by the arguments (per gate id or
    step id: how many times to reject, to fail `until`, to report a wait as
    pending or failed; steps whose `when` is forced false). Conditions are
    otherwise evaluated for real on the fake files. Content contracts and
    checks are not run. Returns a readable trace."""
    rejects = dict(rejects or {})
    trace: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        flow = load_flow(flow_path)
        run = Run.create(flow_path, Path(tmp), "simulation", {k: f"<{k}>" for k in flow.get("inputs", {})})
        run.sim = {"until": dict(until or {}), "skip": set(skip or ()), "wait": dict(waits or {}),
                   "wait_fail": dict(wait_fails or {})}
        seen_events = 0

        def drain_events() -> None:
            nonlocal seen_events
            for e in run._events[seen_events:]:
                if e["event"] == "skip":
                    trace.append(f"skip  {e['step']}  (condition false: {e['condition']})")
                elif e["event"] == "repeat":
                    trace.append(f"loop  {e['step']} -> back to {e['sent_back_to']}  (round {e['round']}: {e['condition']} not met)")
                elif e["event"] == "repeat_exhausted":
                    trace.append(f"loop  {e['step']} gave up repeating -> {e['on_exhausted']}")
                elif e["event"] == "poll":
                    trace.append(f"wait  {e['step']}: {e['status']}")
                elif e["event"] == "wait_failed":
                    trace.append(f"wait  {e['step']} failed -> back to {e['sent_back_to']}")
                elif e["event"] == "wait_timeout":
                    trace.append(f"wait  {e['step']} timed out")
            seen_events = len(run._events)

        for _ in range(max_actions):
            act = run.next_action()
            drain_events()
            if act["action"] == "run_step":
                run.start(act["step"])
                unit = run.resolve(act["step"])
                for ad in unit.unit_def.get("agents") or [None]:
                    for spec in artifact_specs(unit.unit_def, ad) if ad else artifact_specs(unit.unit_def):
                        atomic_write(run.artifacts_dir / run._art(spec["path"], unit), _fake_output(run, unit, spec))
                pi = unit.unit_def.get("produces_items")
                if pi:
                    atomic_write(run.artifacts_dir / pi["file"], json.dumps(fake_items(flow, pi), indent=2))
                if "wait_check" in act:
                    status, _ = run._run_wait_check(unit)
                    atomic_write(Path(act["wait_check"]["status_file"]), json.dumps({"status": status, "summary": "simulated"}))
                if act.get("agents"):
                    where = f"{len(act['agents'])} parallel subagents: {', '.join(a['agent'] for a in act['agents'])}"
                else:
                    where = "subagent" if act["run_in_subagent"] else "inline"
                extras = ""
                if act["feedback"]:
                    extras += " (with feedback)"
                if unit.unit_def.get("checks"):
                    extras += f" + {len(unit.unit_def['checks'])} check(s), not run here"
                trace.append(f"run   {act['step']}  [attempt {act['attempt']}, {where}]{extras}")
                for agent in act.get("agents") or [None]:
                    run.complete(act["step"], agent=agent and agent["agent"])
            elif act["action"] == "wait":
                run.poll(act["step"], force=True)
            elif act["action"] == "await_gate":
                if rejects.get(act["gate"], 0) > 0:
                    rejects[act["gate"]] -= 1
                    res = run.reject(act["gate"], "simulated rejection")
                    trace.append(f"gate  {act['gate']} ({act['when']} {act['step']}) REJECTED -> back to {res['sent_back_to']}")
                else:
                    run.approve(act["gate"])
                    trace.append(f"gate  {act['gate']} ({act['when']} {act['step']}) approved")
            elif act["action"] == "failed":
                trace.append(f"FAILED {act['step']}: {act['reason']}")
                break
            else:
                trace.append("done")
                break
            drain_events()
        else:
            trace.append(f"stopped after {max_actions} actions (endless loop?)")
    return trace


def iter_runs(flow_path: Path, runs_dir: Path) -> Iterator[Run]:
    flow = load_flow(flow_path)
    base = Path(runs_dir) / flow["name"]
    if not base.is_dir():
        return
    for d in sorted(base.iterdir()):
        if (d / "state.json").is_file():
            with contextlib.suppress(EngineError):
                yield Run.load(flow_path, runs_dir, d.name)


def guard(flow_path: Path, runs_dir: Path, hook_input: dict) -> Optional[str]:
    """PreToolUse check. Returns a reason to block, or None to allow.

    A tool call that matches a gate's guard is allowed only while some run has
    that gated step as its current action, which means the gate was approved."""
    flow = load_flow(flow_path)
    tool = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input") or {}
    text = tool_input.get("command") if isinstance(tool_input.get("command"), str) else json.dumps(tool_input)
    guarded = [(u["id"], u["gate"]) for _, u in iter_units(flow) if (u.get("gate") or {}).get("guard")]
    for unit_id, g in guarded:
        if g["guard"].get("tool") and g["guard"]["tool"] != tool:
            continue
        if not re.search(g["guard"]["pattern"], text or ""):
            continue
        for run in iter_runs(flow_path, runs_dir):
            act = run.next_action()
            if act["action"] == "run_step" and re.sub(r".*\.", "", act["step"]) == unit_id:
                return None
        return (f"Blocked by gate {g['id']} of the '{flow['name']}' flow: this action needs the user's approval first. "
                f"Run the flow's orchestrator and get gate {g['id']} approved.")
    return None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def run_lock(run_dir: Path) -> Iterator[None]:
    if fcntl is None or not run_dir.exists():
        yield
        return
    with open(run_dir / ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _counts(specs: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for spec in specs:
        name, _, n = spec.partition("=")
        out[name] = int(n or 1)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="state_manager.py", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--flow", default=str(DEFAULT_FLOW), help="flow definition (default: flow.json next to this script)")
    p.add_argument("--runs-dir", default=os.environ.get("AGENTIC_LOOPS_DIR", DEFAULT_RUNS_DIR),
                   help=f"where runs live (default: $AGENTIC_LOOPS_DIR or {DEFAULT_RUNS_DIR})")
    p.add_argument("--run", help="run id, e.g. the Jira key")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--run", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_, parents=[common])

    i = add("init", "start a run (checks the environment first)")
    i.add_argument("--input", action="append", default=[], metavar="KEY=VALUE")
    i.add_argument("--skip-preflight", action="store_true", help="start even if required commands or variables are missing")
    add("next", "print the single next action")
    add("start", "mark the current step running").add_argument("step")
    c = add("complete", "record a finished step; its output must pass its contract and checks")
    c.add_argument("step")
    c.add_argument("--artifact", action="append", default=[], help="extra artifact path (declared ones are checked automatically)")
    c.add_argument("--summary")
    c.add_argument("--agent", help="which parallel agent finished (steps with 'agents')")
    po = add("poll", "check a waiting step")
    po.add_argument("step")
    po.add_argument("--block", type=int, default=0, metavar="SECONDS",
                    help=f"keep polling up to this long while the check is pending (max {MAX_BLOCK_SECONDS})")
    po.add_argument("--now", action="store_true", help="check even if the next check is not due yet")
    f = add("fail", "mark a step failed")
    f.add_argument("step")
    f.add_argument("--reason", required=True)
    f.add_argument("--agent", help="which parallel agent failed (steps with 'agents')")
    a = add("approve", "approve the waiting gate")
    a.add_argument("target", help="gate id or step path")
    a.add_argument("--note")
    r = add("reject", "reject the waiting gate and send the work back")
    r.add_argument("target", help="gate id or step path")
    r.add_argument("--feedback", required=True)
    add("retry", "re-run a failed step").add_argument("step")
    add("status", "human-readable progress")
    add("show", "raw state JSON")
    add("list", "list runs of this flow")
    add("validate", "check the flow definition")
    add("preflight", "check that the commands, variables and MCP servers the flow needs are available")
    s = add("simulate", "walk the flow with fake artifacts")
    s.add_argument("--reject", action="append", default=[], metavar="GATE[=N]", help="reject GATE the first N times")
    s.add_argument("--until", action="append", default=[], metavar="STEP[=N]", help="make STEP's repeat condition fail N times")
    s.add_argument("--skip", action="append", default=[], metavar="STEP", help="force STEP's `when` condition false")
    s.add_argument("--wait", action="append", default=[], metavar="STEP[=N]", help="report wait STEP as pending N times")
    s.add_argument("--wait-fail", action="append", default=[], metavar="STEP[=N]", help="report wait STEP as failed N times")
    add("guard", "PreToolUse hook: block guarded actions until their gate is approved (reads hook JSON on stdin)")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    flow_path = Path(args.flow)
    runs_dir = Path(args.runs_dir)
    try:
        if args.cmd == "validate":
            load_flow(flow_path)
            return _out({"ok": True, "flow": str(flow_path)})
        if args.cmd == "preflight":
            report = preflight(load_flow(flow_path))
            return _out(report, code=0 if report["ok"] else 1)
        if args.cmd == "simulate":
            print("\n".join(simulate(flow_path, _counts(args.reject), _counts(args.until), set(args.skip),
                                     _counts(args.wait), _counts(args.wait_fail))))
            return 0
        if args.cmd == "guard":
            try:
                hook_input = json.load(sys.stdin)
            except json.JSONDecodeError:
                return 0
            reason = guard(flow_path, runs_dir, hook_input)
            if reason:
                print(reason, file=sys.stderr)
                return 2  # exit code 2 blocks the tool call in Claude Code
            return 0
        if args.cmd == "list":
            return _out({"runs": [
                {"run": r.state["run_id"], "next": _brief(r.next_action()), "updated_at": r.state["updated_at"]}
                for r in iter_runs(flow_path, runs_dir)
            ]})
        if not args.run:
            raise EngineError("this command needs --run <RUN_ID>")
        cli = _cli_prefix(args)
        if args.cmd == "init":
            inputs = {}
            for kv in args.input:
                k, sep, v = kv.partition("=")
                if not sep:
                    raise EngineError(f"--input expects KEY=VALUE, got '{kv}'")
                inputs[k] = v
            run = Run.create(flow_path, runs_dir, args.run, inputs, cli, check_environment=not args.skip_preflight)
            return _out({"ok": True, "run": args.run, "run_dir": str(run.run_dir), "next": run.next_action()})
        flow = load_flow(flow_path)
        with run_lock(Run.dir_for(flow, runs_dir, args.run)):
            run = Run.load(flow_path, runs_dir, args.run, cli)
            if args.cmd == "status":
                text = run.render_status()
                if run.dirty:
                    run.save()
                print(text)
                return 0
            if args.cmd == "show":
                return _out(run.state)
            commands = {
                "next": lambda: run.next_action(),
                "start": lambda: run.start(args.step),
                "complete": lambda: run.complete(args.step, tuple(args.artifact), args.summary, args.agent),
                "poll": lambda: run.poll(args.step, args.block, args.now),
                "fail": lambda: run.fail(args.step, args.reason, args.agent),
                "approve": lambda: run.approve(args.target, args.note),
                "reject": lambda: run.reject(args.target, args.feedback),
                "retry": lambda: run.retry(args.step),
            }
            try:
                result = commands[args.cmd]()
            except VerificationError:
                run.save()  # the refusal was counted
                raise
            if args.cmd != "next" or run.dirty:
                run.save()
            return _out(result)
    except EngineError as e:
        return _out({"ok": False, "error": str(e)}, code=1)


def _cli_prefix(args: argparse.Namespace) -> str:
    parts = ["python3", sys.argv[0] or "state_manager.py"]
    if Path(args.flow).resolve() != DEFAULT_FLOW:
        parts += ["--flow", args.flow]
    if args.runs_dir != DEFAULT_RUNS_DIR:
        parts += ["--runs-dir", args.runs_dir]
    parts += ["--run", args.run]
    return " ".join(shlex.quote(p) for p in parts)


def _out(obj: Any, code: int = 0) -> int:
    print(json.dumps(obj, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
