---
name: agentic-loop-builder
description: Design and generate a gated, resumable agentic workflow (a "loop") as a skill. It produces a flow.json, a state engine, an orchestrator SKILL.md, and one skill per step that runs in a subagent. Use when the user wants to build, scaffold or change a multi-step agent flow with approval gates, such as Jira to requirements to subtasks to implement/review to PR.
argument-hint: "[loop-name] [short description]"
---

# Agentic Loop Builder

You help the user design a **loop**: a multi-step workflow with approval gates. You then generate it as a skill they can run with `/<loop-name> <run-id>`.

A generated loop has three separated parts:

| Part | File | Role |
|---|---|---|
| Flow definition | `flow.json` | Steps, gates, artifacts, loops. Written once per flow type, by you and the user. |
| Run state | `.agentic-loops/<loop>/<run>/state.json` | One run's progress. Written **only** by the engine. |
| Engine | `state_manager.py` | Generic; reads `flow.json` and answers "what's next?" for the orchestrator. |

The tools live next to this file:

- `scaffold.py`: validates a flow and generates the loop skill (`validate`, `schema`, `generate`).
- `state_manager.py`: the engine, copied into every generated loop. `simulate` dry-runs a flow.
- `templates/`: the orchestrator and step-stub templates.

`BUILDER` below means the directory of this file.

## Flow definition reference

```jsonc
{
  "name": "development-loop",            // kebab-case; becomes /development-loop
  "description": "…when to use it…",     // the orchestrator skill's description
  "inputs": { "story": "Jira key" },     // required at `init --input story=…`
  "defaults": { "run_in_subagent": true, "max_iterations": 3 },
  "item_schemas": { "subtask": { /* JSON Schema subset */ } },
  "steps": [
    { "id": "requirements", "kind": "single",
      "skill": "steps/requirements.md",           // step instructions, relative to the loop dir
      "needs": ["jira_context"],                  // earlier steps; rerun together on reject
      "inputs": ["jira_context"],                 // whose artifacts the step reads (default: needs)
      "artifacts": ["requirements.md"],           // must exist when the step completes
      "gate": { "id": "G1", "when": "after",      // after = approve output, before = approve action
                "prompt": "…", "on_reject": "requirements" } },
    { "id": "decomposition", "kind": "single", "skill": "…", "artifacts": ["subtasks.json"],
      "produces_items": { "file": "subtasks.json", "key": "subtasks", "item_schema": "subtask" } },
    { "id": "subtasks", "kind": "foreach", "over": "decomposition",
      "respect_item_dependencies": true,          // order items by their depends_on
      "max_iterations": 3,                        // rounds per item before it fails to the user
      "cycle": [
        { "id": "implement", "skill": "…", "artifacts": ["subtasks/{item}/implementation.md"] },
        { "id": "review", "skill": "…", "artifacts": ["subtasks/{item}/review.md"],
          "gate": { "id": "G3", "on_reject": "implement" } } ] },
    { "id": "open_pr", "kind": "single", "skill": "…", "needs": ["subtasks"],
      "gate": { "id": "G4", "when": "before",
                "guard": { "tool": "Bash", "pattern": "gh\\s+pr\\s+create" } } }
  ]
}
```

Semantics, which you should explain to the user as they become relevant:

- **Order.** Steps run one at a time, in listed order. `needs` and `inputs` can only reference earlier steps.
- **`after` gates.** The step runs, then the user approves or rejects its artifacts.
- **`before` gates.** The user approves before the step runs; use these for irreversible actions. Without `on_reject`, rejecting a before-gate halts the run so the user can decide.
- **Rejection.** A reject sends the work back to `on_reject` (default: the gated step itself) with the user's feedback attached. That target is rerun, along with everything that depends on it. In a cycle, everything from the target to the end of the cycle is rerun.
- **`max_iterations`.** After this many attempts at the reject target, the step is marked `failed` and the user decides (`retry` or stop).
- **`foreach` over `produces_items`.** The producing step's file is validated against `item_schema`, its `depends_on` ids are checked and cycle-checked, and the file is expanded into one cycle per item. `{item}` in artifact names becomes the item id.
- **`guard`.** A `PreToolUse` hook blocks matching tool calls unless the gated step is the run's current step. This makes the gate hard rather than prompt-only.
- **`run_in_subagent`** (default true). The orchestrator spawns one subagent per step, so the main context stays small.

## Process

Go phase by phase. Don't skip ahead, and confirm each phase with the user before moving on.

### 1. Understand the flow

Ask the user to describe the workflow in plain words: what goes in, what comes out, and where a human must say yes. If they gave a description as arguments, start from that. Ask about:

- **Inputs.** What identifies a run, such as a Jira key, a ticket URL or a document path?
- **Steps.** For each step: what it produces, as files, and what it needs from earlier steps.
- **Repetition.** Does anything repeat per item (subtasks, files, services)? What decides the items, and do they depend on each other?
- **Loops.** Which review sends work back, and to which step? How many rounds before a human steps in?
- **Gates.** Which outputs does the human approve (`after`)? Which actions need approval before they happen (`before`), such as pushing, deploying, sending or deleting? Which of those must be hard-blocked by a hook?
- **Tools and access** each step needs, such as the Jira MCP, `gh`, or read-only access.

### 2. Confirm a step table

Present a table and iterate until the user agrees:

| # | Step id | Kind | Needs | Artifacts | Gate | On reject | Subagent |
|---|---|---|---|---|---|---|---|

Call out anything risky:

- an irreversible action without a `before` gate;
- a loop without a gate or a cap;
- a foreach whose items have no verification points;
- a step that produces nothing a later step reads.

### 3. Write and validate `flow.json`

Ask where the loop should live; the default is `.claude/skills/<name>/`. Write `flow.json` there, then run:

```
python3 BUILDER/scaffold.py validate <loop-dir>/flow.json
python3 BUILDER/state_manager.py --flow <loop-dir>/flow.json simulate --reject <a gate id>
```

Show the user the simulation trace: it's the exact order of steps and gates their runs will follow, including what happens on a rejection. Fix the flow until both the validation and the trace look right.

### 4. Generate

```
python3 BUILDER/scaffold.py generate <loop-dir>/flow.json --out <loop-dir> [--install-path <path from project root>]
```

This writes `SKILL.md` (the orchestrator), `state.schema.json`, `state_manager.py`, and stubs for every `steps/*.md`. Existing step files and `SKILL.md` are kept; pass `--force` to regenerate `SKILL.md`.

If the output contains `hook_settings`, offer to merge it into `.claude/settings.json`, which turns guarded gates into hard blocks. Show the user the change before writing it. Also suggest adding `.agentic-loops/` to `.gitignore`.

### 5. Write the step skills together

The stubs only have the sections: Goal, Inputs, Outputs, Procedure, Done when, and Constraints. **The quality of the loop comes from this content, so don't rush it.** Fill in one step at a time with the user. For each step, settle:

- the exact structure of each output file, including what the gate reviewer needs to see in it;
- the procedure, including which tools to use and how;
- the done-criteria, which must be checkable by someone else; these are what the gate is judged on;
- the constraints, such as read-only steps, never pushing, and never weakening tests;
- for item-producing steps, the item schema, together with verification points that can actually be run.

For fully written examples worth borrowing from, see `examples/development-loop/steps/` in the agentic-loop-builder repository.

### 6. Hand over

Tell the user:

- how to run the loop: `/<name> <run-id>`;
- where runs live;
- that re-running the same command resumes a run;
- that `state_manager.py --run <id> status` shows progress at any time.

If they change the flow later, edit `flow.json`, re-validate, re-simulate and re-generate. Step files are never overwritten.

## Rules

- Keep one engine. Never hand-write a per-flow state manager or write state-handling code into the orchestrator; if the engine is missing a feature, say so instead.
- Gates exist for the human. Don't design flows where the agent approves its own gates.
- Prefer a few meaningful steps over many thin ones. Each step costs a subagent and a handoff.
