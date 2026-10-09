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
    state_manager.py --run PROJ-123 complete <step> [--artifact PATH ...] [--summary TEXT]
    state_manager.py --run PROJ-123 approve  <gate-or-step> [--note TEXT]
    state_manager.py --run PROJ-123 reject   <gate-or-step> --feedback TEXT
    state_manager.py --run PROJ-123 status

Step paths are `step_id` for single steps and `step_id[ITEM].cycle_step_id` for
the cycle steps of a foreach, e.g. `subtasks[T2].implement`.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, NamedTuple, Optional

try:
    import fcntl
except ImportError:  # Windows: no advisory locking, single orchestrator assumed.
    fcntl = None  # type: ignore[assignment]

STATE_VERSION = 1
DEFAULT_MAX_ITERATIONS = 3
DEFAULT_RUNS_DIR = ".agentic-loops"
DEFAULT_FLOW = Path(__file__).resolve().with_name("flow.json")

FLOW_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PATH_RE = re.compile(
    r"^(?P<step>[a-z][a-z0-9_]*)(?:\[(?P<item>[A-Za-z0-9][A-Za-z0-9_.-]*)\]\.(?P<sub>[a-z][a-z0-9_]*))?$"
)


class EngineError(Exception):
    """A refused command. The message is written for the agent that sent it."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


# --------------------------------------------------------------------------- #
# Flow definition
# --------------------------------------------------------------------------- #

def load_flow(path: Path) -> dict:
    flow = read_json(path, "flow definition")
    errors = validate_flow(flow)
    if errors:
        raise EngineError("invalid flow definition:\n- " + "\n- ".join(errors))
    return flow


def validate_flow(flow: Any) -> list[str]:
    """Return every structural problem in a flow definition (empty when valid)."""
    if not isinstance(flow, dict):
        return ["flow must be a JSON object"]
    errors: list[str] = []
    if not isinstance(flow.get("name"), str) or not FLOW_NAME_RE.match(flow["name"]):
        errors.append("'name' must be kebab-case, e.g. 'development-loop'")
    if not isinstance(flow.get("inputs", {}), dict):
        errors.append("'inputs' must map input names to descriptions")
    schemas = flow.get("item_schemas", {})
    if not isinstance(schemas, dict) or not all(isinstance(s, dict) for s in schemas.values()):
        errors.append("'item_schemas' must map names to JSON Schemas")
        schemas = {}
    defaults = flow.get("defaults", {})
    if not isinstance(defaults, dict):
        errors.append("'defaults' must be an object")
        defaults = {}
    _check_options(defaults, "defaults", errors)

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
            _check_unit(step, where, errors, gate_ids)
            gate = step.get("gate")
            if isinstance(gate, dict) and "on_reject" in gate:
                target = gate["on_reject"]
                if target != sid and (target not in seen or seen[target].get("kind", "single") != "single"):
                    errors.append(f"{where}: gate on_reject '{target}' must be this step or an earlier single step")
            pi = step.get("produces_items")
            if pi is not None:
                if not isinstance(pi, dict) or not isinstance(pi.get("file"), str):
                    errors.append(f"{where}: 'produces_items' needs a 'file'")
                else:
                    if pi["file"] not in step.get("artifacts", []):
                        errors.append(f"{where}: produces_items file '{pi['file']}' must be listed in 'artifacts'")
                    if "item_schema" in pi and pi["item_schema"] not in schemas:
                        errors.append(f"{where}: item_schema '{pi['item_schema']}' is not defined in 'item_schemas'")
        elif kind == "foreach":
            over = step.get("over")
            if over not in seen:
                errors.append(f"{where}: 'over' must name an earlier step")
            elif not seen[over].get("produces_items"):
                errors.append(f"{where}: 'over' step '{over}' has no 'produces_items'")
            if "gate" in step:
                errors.append(f"{where}: a foreach step cannot have a gate; put it on a cycle step")
            if "produces_items" in step:
                errors.append(f"{where}: a foreach step cannot produce items")
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
                if sub["id"] in sub_ids:
                    errors.append(f"{sub_where}: duplicate id")
                _check_options(sub, sub_where, errors)
                _check_unit(sub, sub_where, errors, gate_ids)
                gate = sub.get("gate")
                if isinstance(gate, dict) and "on_reject" in gate and gate["on_reject"] not in sub_ids + [sub["id"]]:
                    errors.append(f"{sub_where}: gate on_reject '{gate['on_reject']}' must be this or an earlier cycle step")
                sub_ids.append(sub["id"])
        else:
            errors.append(f"{where}: unknown kind '{kind}' (use 'single' or 'foreach')")
        seen[sid] = step
    return errors


def _check_options(obj: dict, where: str, errors: list[str]) -> None:
    mi = obj.get("max_iterations")
    if mi is not None and (not isinstance(mi, int) or isinstance(mi, bool) or mi < 1):
        errors.append(f"{where}: 'max_iterations' must be a positive integer")
    if "run_in_subagent" in obj and not isinstance(obj["run_in_subagent"], bool):
        errors.append(f"{where}: 'run_in_subagent' must be true or false")


def _check_unit(unit: dict, where: str, errors: list[str], gate_ids: set[str]) -> None:
    if not isinstance(unit.get("skill"), str) or not unit["skill"]:
        errors.append(f"{where}: 'skill' (path of the step's instructions) is required")
    arts = unit.get("artifacts", [])
    if not isinstance(arts, list) or not all(
        isinstance(a, str) and a and not a.startswith("/") and ".." not in Path(a).parts for a in arts
    ):
        errors.append(f"{where}: 'artifacts' must be a list of relative file names")
        arts = []
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
    if when == "after" and not arts:
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


# --------------------------------------------------------------------------- #
# Items (the output of a produces_items step, consumed by a foreach)
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
    """Validate against the JSON Schema subset item schemas use: type, enum,
    required, properties, additionalProperties: false, items, minItems, minLength."""
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_TYPES.get(x, lambda v: True)(value) for x in types):
            return [f"{where}: expected {' or '.join(types)}"]
    errors = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: must be one of {schema['enum']}")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        errors.append(f"{where}: must have at least {schema['minLength']} characters")
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
    schema = flow.get("item_schemas", {}).get(pi["item_schema"]) if pi.get("item_schema") else None
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
        raise EngineError(f"{pi['file']} is invalid (fix it, then complete the step again):\n- " + "\n- ".join(errors))
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
# Run state
# --------------------------------------------------------------------------- #

def fresh_unit(unit_def: dict) -> dict:
    unit = {
        "status": "pending",  # pending | running | awaiting_gate | done | failed
        "attempt": 0,
        "artifacts": [],
        "previous_artifacts": [],
        "summary": None,
        "feedback": None,
        "feedback_history": [],
        "error": None,
        "started_at": None,
        "completed_at": None,
    }
    gate = unit_def.get("gate")
    if gate:
        unit["gate"] = {"id": gate["id"], "when": gate.get("when", "after"), "status": "pending", "decisions": []}
    return unit


def fresh_step_state(step_def: dict) -> dict:
    if step_def.get("kind") == "foreach":
        return {"kind": "foreach", "status": "pending", "items": None}
    return {"kind": "single", **fresh_unit(step_def)}


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
        self._events: list[dict] = []
        for sid, sdef in self.steps.items():  # tolerate steps added to the flow mid-run
            self.state["steps"].setdefault(sid, fresh_step_state(sdef))

    # ---- persistence ------------------------------------------------------ #

    @staticmethod
    def dir_for(flow: dict, runs_dir: Path, run_id: str) -> Path:
        if not isinstance(run_id, str) or not ITEM_ID_RE.match(run_id):
            raise EngineError(f"bad run id '{run_id}': use letters, digits, '.', '_' or '-', e.g. PROJ-123")
        return Path(runs_dir) / flow["name"] / run_id

    @classmethod
    def create(cls, flow_path: Path, runs_dir: Path, run_id: str, inputs: dict, cli: Optional[str] = None) -> "Run":
        flow = load_flow(flow_path)
        missing = [k for k in flow.get("inputs", {}) if k not in inputs]
        if missing:
            raise EngineError("missing inputs: " + ", ".join(f"--input {k}=..." for k in missing))
        run_dir = cls.dir_for(flow, runs_dir, run_id)
        if (run_dir / "state.json").exists():
            raise EngineError(f"run '{run_id}' already exists at {run_dir}; call `next` to resume it")
        state = {
            "version": STATE_VERSION,
            "flow": flow["name"],
            "run_id": run_id,
            "inputs": inputs,
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

    def _art(self, name: str, unit: Unit) -> str:
        return name.replace("{item}", unit.item["id"]) if unit.item else name

    def _abs(self, p: str) -> str:
        return p if os.path.isabs(p) else str(self.artifacts_dir / p)

    def _max_iterations(self, unit_def: dict, step_def: dict) -> int:
        for src in (unit_def, step_def, self.flow.get("defaults", {})):
            if "max_iterations" in src:
                return src["max_iterations"]
        return DEFAULT_MAX_ITERATIONS

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
        return list(dict.fromkeys(self._abs(p) for p in paths))

    # ---- deciding what's next --------------------------------------------- #

    def next_action(self) -> dict:
        for sdef in self.flow["steps"]:
            st = self.state["steps"][sdef["id"]]
            if st["status"] == "done":
                continue
            if sdef.get("kind") == "foreach":
                unit = self._foreach_current(sdef, st)
                if unit is None:
                    continue
            else:
                unit = Unit(sdef["id"], sdef, sdef, st, None)
            return self._action_for(unit)
        return {
            "action": "done",
            "run": self.state["run_id"],
            "run_dir": str(self.run_dir),
            "artifacts": list(dict.fromkeys(self._abs(p) for sid in self.steps for p in self._step_artifacts(sid))),
        }

    def _foreach_current(self, sdef: dict, st: dict) -> Optional[Unit]:
        if st["items"] is None:
            self._expand(sdef, st)
        items = st["items"]
        if all(it["status"] == "done" for it in items):
            st["status"] = "done"
            self._event("step_done", step=sdef["id"])
            return None
        if st["status"] == "pending":
            st["status"] = "running"
        done_ids = {it["id"] for it in items if it["status"] == "done"}
        respect_deps = sdef.get("respect_item_dependencies", True)
        for it in items:
            if it["status"] == "done":
                continue
            if respect_deps and not set(it["depends_on"]) <= done_ids:
                continue
            for sub in sdef["cycle"]:
                sst = it["steps"][sub["id"]]
                if sst["status"] != "done":
                    return Unit(f"{sdef['id']}[{it['id']}].{sub['id']}", sdef, sub, sst, it)
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

    def _action_for(self, unit: Unit) -> dict:
        st = unit.st
        gate = st.get("gate")
        if st["status"] == "failed":
            return {
                "action": "failed",
                "step": unit.path,
                "reason": st["error"],
                "feedback": st["feedback"],
                "hint": "Show the reason to the user. `retry` resets the attempt counter and runs the step again; feedback is kept.",
                "commands": {"retry": f"{self.cli} retry {shlex.quote(unit.path)}"},
            }
        if st["status"] == "awaiting_gate" or (
            st["status"] == "pending" and gate and gate["when"] == "before" and gate["status"] != "approved"
        ):
            return self._gate_action(unit)
        return self._run_action(unit)

    def _run_action(self, unit: Unit) -> dict:
        ud, st = unit.unit_def, unit.st
        defaults = self.flow.get("defaults", {})
        running = st["status"] == "running"
        act: dict[str, Any] = {
            "action": "run_step",
            "step": unit.path,
            "skill": str(self.flow_path.parent / ud["skill"]),
            "run_in_subagent": ud.get("run_in_subagent", unit.step_def.get("run_in_subagent", defaults.get("run_in_subagent", True))),
            "attempt": st["attempt"] if running else st["attempt"] + 1,
            "resumed": running,
            "inputs": self._inputs(unit),
            "outputs": [self._abs(self._art(a, unit)) for a in ud.get("artifacts", [])],
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
                act["item_schema"] = self.flow["item_schemas"][pi["item_schema"]]
        act["commands"] = {
            "start": f"{self.cli} start {shlex.quote(unit.path)}",
            "complete": f"{self.cli} complete {shlex.quote(unit.path)} --summary '<one line>'",
            "fail": f"{self.cli} fail {shlex.quote(unit.path)} --reason '<why it cannot be done>'",
        }
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

    # ---- transitions -------------------------------------------------------- #

    def start(self, path: str) -> dict:
        unit = self.resolve(path)
        st = unit.st
        if st["status"] != "running":
            act = self.next_action()
            if act["action"] != "run_step" or act["step"] != unit.path:
                raise EngineError(f"'{unit.path}' cannot start now; the next action is {_brief(act)}")
            st.update(status="running", attempt=st["attempt"] + 1, started_at=now(), completed_at=None, error=None)
            self._refresh_item(unit)
            self._event("start", step=unit.path, attempt=st["attempt"])
        return {"ok": True, "step": unit.path, "status": "running", "attempt": st["attempt"]}

    def complete(self, path: str, extra: tuple = (), summary: Optional[str] = None) -> dict:
        unit = self.resolve(path)
        st = unit.st
        if st["status"] != "running":
            raise EngineError(f"'{unit.path}' is {st['status']}, not running; call `start {unit.path}` first")
        arts: list[str] = []
        missing: list[str] = []
        for name in unit.unit_def.get("artifacts", []):
            rel = self._art(name, unit)
            if not (self.artifacts_dir / rel).is_file():
                missing.append(str(self.artifacts_dir / rel))
            arts.append(rel)
        for p in extra:
            pp = Path(p)
            if not pp.is_absolute():
                pp = self.artifacts_dir / pp if (self.artifacts_dir / pp).exists() else Path.cwd() / pp
            pp = pp.resolve()
            if not pp.exists():
                missing.append(str(p))
                continue
            try:
                arts.append(str(pp.relative_to(self.artifacts_dir)))
            except ValueError:
                arts.append(str(pp))
        if missing:
            raise EngineError("missing artifacts (write them, then complete again):\n- " + "\n- ".join(missing))
        pi = unit.unit_def.get("produces_items")
        if pi:
            check_items(self.flow, pi, read_json(self.artifacts_dir / pi["file"], pi["file"]))
        st.update(artifacts=list(dict.fromkeys(arts)), summary=summary, completed_at=now())
        gate = st.get("gate")
        if gate and gate["when"] == "after":
            st["status"] = "awaiting_gate"
            gate["status"] = "pending"
        else:
            st["status"] = "done"
            st["feedback"] = None
        self._refresh_item(unit)
        self._event("complete", step=unit.path, artifacts=st["artifacts"], summary=summary)
        return {"ok": True, "step": unit.path, "status": st["status"], "next": self.next_action()}

    def fail(self, path: str, reason: str) -> dict:
        unit = self.resolve(path)
        if unit.st["status"] != "running":
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
            unit.st["status"] = "done"
            unit.st["feedback"] = None
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
        message = f"Gate {gate['id']} rejected: {feedback}"
        on_reject = gdef.get("on_reject")
        if gate["when"] == "before" and not on_reject:
            unit.st.update(status="failed", error=f"{message} (the gate is before the step, so nothing ran)", feedback=message)
            unit.st["feedback_history"].append({"at": now(), "feedback": message})
            sent_back = unit.path
        elif unit.item:
            sent_back = self._send_back_in_cycle(unit, on_reject or unit.unit_def["id"], message)
        else:
            sent_back = self._send_back(unit, on_reject or unit.unit_def["id"], message)
        self._refresh_item(unit)
        self._event("reject", gate=gate["id"], step=unit.path, feedback=feedback, sent_back_to=sent_back)
        return {"ok": True, "gate": gate["id"], "step": unit.path, "sent_back_to": sent_back, "next": self.next_action()}

    def retry(self, path: str) -> dict:
        unit = self.resolve(path)
        if unit.st["status"] != "failed":
            raise EngineError(f"'{unit.path}' is {unit.st['status']}; only a failed step can be retried")
        unit.st.update(status="pending", attempt=0, error=None)
        if unit.st.get("gate") and unit.st["gate"]["status"] == "rejected":
            unit.st["gate"]["status"] = "pending"
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

    def _send_back_in_cycle(self, unit: Unit, target_id: str, message: str) -> str:
        cycle = unit.step_def["cycle"]
        idx = next(i for i, s in enumerate(cycle) if s["id"] == target_id)
        for sub in cycle[idx:]:
            _reset_unit(unit.item["steps"][sub["id"]])
        target = unit.item["steps"][target_id]
        self._give_feedback(target, message, self._max_iterations(cycle[idx], unit.step_def))
        return f"{unit.step_def['id']}[{unit.item['id']}].{target_id}"

    def _send_back(self, unit: Unit, target_id: str, message: str) -> str:
        reset = {target_id, unit.step_def["id"]} | self._dependents(target_id)
        for sid in reset:
            sdef = self.steps[sid]
            if sdef.get("kind") == "foreach":
                self.state["steps"][sid] = fresh_step_state(sdef)  # items are re-read on the next pass
            else:
                _reset_unit(self.state["steps"][sid])
        tdef = self.steps[target_id]
        self._give_feedback(self.state["steps"][target_id], message, self._max_iterations(tdef, tdef))
        return target_id

    def _dependents(self, sid: str) -> set[str]:
        found = {sid}
        for sdef in self.flow["steps"]:
            refs = set(sdef.get("needs", [])) | set(sdef.get("inputs", [])) | ({sdef["over"]} if "over" in sdef else set())
            if refs & found:
                found.add(sdef["id"])
        return found - {sid}

    @staticmethod
    def _give_feedback(st: dict, message: str, max_iterations: int) -> None:
        st["feedback"] = message
        st["feedback_history"].append({"at": now(), "feedback": message})
        if st["attempt"] >= max_iterations:
            st["status"] = "failed"
            st["error"] = f"still rejected after {st['attempt']} attempt(s) (max_iterations={max_iterations}). {message}"

    @staticmethod
    def _refresh_item(unit: Unit) -> None:
        if not unit.item:
            return
        statuses = [s["status"] for s in unit.item["steps"].values()]
        if all(s == "done" for s in statuses):
            unit.item["status"] = "done"
        elif "failed" in statuses:
            unit.item["status"] = "failed"
        elif all(s == "pending" for s in statuses):
            unit.item["status"] = "pending"
        else:
            unit.item["status"] = "running"

    # ---- reporting ---------------------------------------------------------- #

    def render_status(self) -> str:
        marks = {"done": "x", "running": ">", "awaiting_gate": "?", "failed": "!", "pending": " "}
        act = self.next_action()
        lines = [f"{self.flow['name']} · run {self.state['run_id']} · next: {_brief(act)}", ""]

        def unit_line(indent: str, label: str, st: dict) -> str:
            info = st["status"]
            if st.get("gate"):
                g = st["gate"]
                info += f" · gate {g['id']} ({g['when']}) {g['status']}"
            if st["attempt"] > 1:
                info += f" · attempt {st['attempt']}"
            if st["feedback"] and st["status"] != "failed":
                info += " · has feedback"
            if st["status"] == "failed" and st["error"]:
                info += f" · {st['error']}"
            return f"{indent}[{marks[st['status']]}] {label:<24} {info}"

        for sdef in self.flow["steps"]:
            st = self.state["steps"][sdef["id"]]
            if sdef.get("kind") != "foreach":
                lines.append(unit_line("", sdef["id"], st))
                continue
            items = st["items"]
            count = f"{sum(i['status'] == 'done' for i in items)}/{len(items)} items done" if items is not None else "not expanded yet"
            lines.append(f"[{marks.get(st['status'], ' ')}] {sdef['id']:<24} foreach over {sdef['over']} · {count}")
            for it in items or []:
                deps = f" (after {', '.join(it['depends_on'])})" if it["depends_on"] else ""
                lines.append(f"    {it['id']}: {it.get('title') or ''}{deps}")
                for sub in sdef["cycle"]:
                    lines.append(unit_line("      ", sub["id"], it["steps"][sub["id"]]))
        return "\n".join(lines)


def _reset_unit(st: dict) -> None:
    if st["artifacts"]:
        st["previous_artifacts"] = st["artifacts"]
    st.update(status="pending", artifacts=[], summary=None, error=None, started_at=None, completed_at=None)
    if st.get("gate"):
        st["gate"]["status"] = "pending"


def _brief(act: dict) -> str:
    if act["action"] == "run_step":
        return f"run_step {act['step']}"
    if act["action"] == "await_gate":
        return f"await_gate {act['gate']} on {act['step']}"
    if act["action"] == "failed":
        return f"failed {act['step']}"
    return act["action"]


# --------------------------------------------------------------------------- #
# Simulation, guard hook, run listing
# --------------------------------------------------------------------------- #

def _fake_value(schema: dict) -> Any:
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type", "string")
    t = t[0] if isinstance(t, list) else t
    if t == "object":
        props = schema.get("properties", {})
        return {k: _fake_value(props.get(k, {})) for k in schema.get("required", list(props))}
    if t == "array":
        return [_fake_value(schema.get("items", {})) for _ in range(max(1, schema.get("minItems", 1)))]
    return {"integer": 1, "number": 1, "boolean": True, "null": None}.get(t, "simulated")


def fake_items(flow: dict, pi: dict, count: int = 2) -> Any:
    schema = flow.get("item_schemas", {}).get(pi.get("item_schema"), {})
    id_field = pi.get("id_field", "id")
    dep_field = pi.get("depends_on_field", "depends_on")
    items = []
    for n in range(1, count + 1):
        item = _fake_value({"type": "object", **schema}) if schema else {}
        item[id_field] = f"T{n}"
        item[dep_field] = [f"T{n - 1}"] if n > 1 else []
        items.append(item)
    return {pi["key"]: items} if pi.get("key") else items


def simulate(flow_path: Path, rejects: Optional[dict[str, int]] = None, max_actions: int = 500) -> list[str]:
    """Walk a flow with fake artifacts, approving every gate except the
    requested rejections, and return a readable trace."""
    rejects = dict(rejects or {})
    trace: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        flow = load_flow(flow_path)
        run = Run.create(flow_path, Path(tmp), "simulation", {k: f"<{k}>" for k in flow.get("inputs", {})})
        for _ in range(max_actions):
            act = run.next_action()
            if act["action"] == "run_step":
                run.start(act["step"])
                for out in act["outputs"]:
                    content = json.dumps(fake_items(flow, run.resolve(act["step"]).unit_def["produces_items"]), indent=2) \
                        if out == act.get("items_file") else "simulated\n"
                    atomic_write(Path(out), content)
                where = "subagent" if act["run_in_subagent"] else "inline"
                fb = " (with feedback)" if act["feedback"] else ""
                trace.append(f"run   {act['step']}  [attempt {act['attempt']}, {where}]{fb}")
                run.complete(act["step"])
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
    guarded = []
    for sdef in flow["steps"]:
        for udef in sdef.get("cycle", [sdef]):
            g = udef.get("gate") or {}
            if g.get("guard"):
                guarded.append((udef["id"], g))
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

    add("init", "start a run").add_argument("--input", action="append", default=[], metavar="KEY=VALUE")
    add("next", "print the single next action")
    add("start", "mark the current step running").add_argument("step")
    c = add("complete", "record a finished step and its artifacts")
    c.add_argument("step")
    c.add_argument("--artifact", action="append", default=[], help="extra artifact path (declared ones are checked automatically)")
    c.add_argument("--summary")
    f = add("fail", "mark a step failed")
    f.add_argument("step")
    f.add_argument("--reason", required=True)
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
    s = add("simulate", "walk the flow with fake artifacts")
    s.add_argument("--reject", action="append", default=[], metavar="GATE[=N]", help="reject GATE the first N times (default 1)")
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
        if args.cmd == "simulate":
            rejects = {}
            for spec in args.reject:
                gate, _, n = spec.partition("=")
                rejects[gate] = int(n or 1)
            print("\n".join(simulate(flow_path, rejects)))
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
            run = Run.create(flow_path, runs_dir, args.run, inputs, cli)
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
            result = {
                "next": lambda: run.next_action(),
                "start": lambda: run.start(args.step),
                "complete": lambda: run.complete(args.step, tuple(args.artifact), args.summary),
                "fail": lambda: run.fail(args.step, args.reason),
                "approve": lambda: run.approve(args.target, args.note),
                "reject": lambda: run.reject(args.target, args.feedback),
                "retry": lambda: run.retry(args.step),
            }[args.cmd]()
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
