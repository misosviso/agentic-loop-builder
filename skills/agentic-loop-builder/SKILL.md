---
name: agentic-loop-builder
description: "Design and generate a gated multi-step agent workflow: a /command, an orchestrator agent and per-step subagents."
argument-hint: "[loop-name] [short description]"
---

# Agentic Loop Builder

You help the user design a **loop**: a multi-step workflow with approval gates. You then generate it as:

- **a command**, `/<loop-name> <run-id>`. Running it hands the run straight to the flow's orchestrator agent.
- **an orchestrator agent**, which only coordinates: it launches a subagent for each step (several in parallel where the flow says so) and stops at every gate. It can also run as its own session with `claude --agent <loop-name>`.
- **optional custom subagents** for steps that need a specialist, such as an implementer that writes code or a Jira fetcher. Steps without one use the generic `general-purpose` agent.

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
  "description": "…one short sentence…", // the command's description, under 200 characters
  "inputs": { "story": "Jira key" },     // required at `init --input story=…`
  "defaults": { "run_in_subagent": true, "max_iterations": 3, "subagent_type": "general-purpose" },
  "orchestrator": { "model": "inherit", "color": "blue" },   // frontmatter of the generated agent
  "subagents": {                         // optional custom subagents, generated into .claude/agents/
    "development-loop-implementer": {
      "description": "Implements one coding subtask with tests and commits it.",
      "tools": ["Read", "Write", "Edit", "Glob", "Grep", "Bash"] } },  // + model, disallowedTools, …
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
        { "id": "implement", "skill": "…", "subagent_type": "development-loop-implementer",
          "artifacts": ["subtasks/{item}/implementation.md"] },
        { "id": "review", "skill": "steps/review.md",
          "agents": [                             // parallel subagents for this one step
            { "id": "correctness", "focus": "bugs, edge cases, security" },
            { "id": "verification", "focus": "re-run checks, judge the tests",
              "skill": "steps/review-tests.md", "subagent_type": "test-runner" } ],
          "artifacts": ["subtasks/{item}/review-{agent}.md"],   // {agent} -> the agent id
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
- **`agents`** (parallel subagents). The step launches every listed agent at once. Each gets the step's inputs plus its own `focus`, its own outputs (an `agents[].artifacts` list, or the step's `artifacts` with `{agent}` replaced) and, optionally, its own `skill` and `subagent_type`. The engine waits for every agent to report, then the step completes, so the gate sees all their outputs side by side. If one agent fails, `retry` reruns only the agents that didn't finish. Typical uses are several reviewers with different lenses, independent research angles, or an implementation compared against a second opinion. Parallel agents share the working tree, so give them read-only work or disjoint output files.
- **`subagent_type`** (default `general-purpose`). Which Claude Code agent runs the step. It can be set per step, per parallel agent, or in `defaults`. It names either one of the flow's own `subagents` or an agent that already exists in `.claude/agents/`. The orchestrator agent may only launch the types the flow uses.
- **`subagents`**: custom subagents the flow defines. Each one is generated as `.claude/agents/<name>.md`. Its frontmatter comes from `flow.json`: `description`, plus `tools`, `disallowedTools`, `model`, `color`, `effort`, `maxTurns`, `permissionMode`, `isolation`, `skills` or `mcpServers`. Its prompt (the body) is written with the user and kept on regeneration. Prefix the names with the flow name to avoid clashes, e.g. `development-loop-jira`.
- **`orchestrator`**: `model`, `color` and `effort` for the generated orchestrator agent.

## Process

Go phase by phase. Don't skip ahead, and confirm each phase with the user before moving on.

### 1. Understand the flow

Ask the user to describe the workflow in plain words: what goes in, what comes out, and where a human must say yes. If they gave a description as arguments, start from that. Ask about:

- **Inputs.** What identifies a run, such as a Jira key, a ticket URL or a document path?
- **Steps.** For each step: what it produces, as files, and what it needs from earlier steps.
- **Repetition.** Does anything repeat per item (subtasks, files, services)? What decides the items, and do they depend on each other?
- **Loops.** Which review sends work back, and to which step? How many rounds before a human steps in?
- **Parallelism.** Would any step benefit from several subagents working at once, such as two reviewers with different focuses? What does each one own, and which file does each write?
- **Gates.** Which outputs does the human approve (`after`)? Which actions need approval before they happen (`before`), such as pushing, deploying, sending or deleting? Which of those must be hard-blocked by a hook?
- **Tools and access** each step needs, such as the Jira MCP, `gh`, or read-only access.
- **Custom subagents.** For **each step**, ask whether it should get its own specialist subagent or use the generic `general-purpose` one, and make the generic one the default. A custom subagent is worth it when the step needs:
  - different tools, such as a read-only reviewer, an implementer with write access, or a Jira fetcher with the Jira MCP;
  - a different model, such as a cheap `haiku` for fetching data;
  - a persona it should carry everywhere, such as "senior engineer in this codebase".

  One custom subagent can serve several steps. If the user already has a suitable agent in `.claude/agents/`, reference it by name instead of defining a new one.

### 2. Confirm a step table

Present a table and iterate until the user agrees:

| # | Step id | Kind | Needs | Artifacts | Gate | On reject | Subagent (generic or custom name; parallel agents) |
|---|---|---|---|---|---|---|---|

Then list the custom subagents, if any, in a second table:

| Name | Used by | What it is good at | Tools | Model |
|---|---|---|---|---|

Call out anything risky:

- an irreversible action without a `before` gate;
- a loop without a gate or a cap;
- a foreach whose items have no verification points;
- a step that produces nothing a later step reads;
- parallel agents that would edit the same files;
- a custom subagent with more tools than its steps need, such as write access for a reviewer.

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

This writes:

- **`SKILL.md`, the `/<name>` command.** It uses `context: fork` with `agent: <name>`, so running the command starts the orchestrator agent.
  - The agent applies the decision passed in the arguments, if any.
  - It then advances the run to the next gate and returns a report, with the exact commands the user types next: `/<name> <run> approve <gate>` or `/<name> <run> reject <gate> <feedback>`.
  - The command has `disable-model-invocation: true`, so only the user can run it. Claude can't record a gate decision on the user's behalf.
- **The orchestrator agent**, at `.claude/agents/<name>.md`. Choose another path with `--agent-path`. With `--no-agent`, no agent is generated and the command orchestrates inline instead.
  - Its tools are `Agent(<the subagent types the flow uses>)`, Bash for the engine, Read, Glob, Grep and AskUserQuestion. It has no editing tools, so it can't do a step's work itself.
- **One file per custom subagent**, at `.claude/agents/<subagent>.md`, with a stub prompt.
- **`state.schema.json` and `state_manager.py`.**
- **Stubs for every `steps/*.md`**, including each parallel agent's own skill.

On regeneration:

- step files are kept;
- `SKILL.md` and the orchestrator agent are kept unless you pass `--force`;
- a custom subagent's frontmatter is re-synced from `flow.json`, and its prompt is kept.

If the output contains `hook_settings`, offer to merge it into `.claude/settings.json`, which turns guarded gates into hard blocks. Show the user the change before writing it. Also suggest adding `.agentic-loops/` to `.gitignore`.

### 5. Write the step skills together

The stubs only have the sections: Goal, Inputs, Outputs, Procedure, Done when, and Constraints. **The quality of the loop comes from this content, so don't rush it.** Fill in one step at a time with the user. For each step, settle:

- the exact structure of each output file, including what the gate reviewer needs to see in it;
- the procedure, including which tools to use and how;
- the done-criteria, which must be checkable by someone else; these are what the gate is judged on;
- the constraints, such as read-only steps, never pushing, and never weakening tests;
- for item-producing steps, the item schema, together with verification points that can actually be run.

Then fill in the **Role** section of each custom subagent's prompt in the same way. That section says what it is an expert at, the conventions it follows, and its hard limits; for example, "never pushes" or "read-only towards Jira". The step file says *what* to produce; the subagent's prompt says *how* this kind of work is done well.

For fully written examples worth borrowing from, see `examples/development-loop/.claude/` in the agentic-loop-builder repository: the step skills, plus the `development-loop-jira` and `development-loop-implementer` subagents.

### 6. Hand over

Tell the user:

- how to run the loop: `/<name> <run-id>` advances the run to the next gate. They answer each gate with `/<name> <run-id> approve <gate>` or `reject <gate> <feedback>`. Alternatively, `claude --agent <name>` runs the orchestrator as an interactive session that asks at each gate.
- where runs live;
- that the same command resumes an interrupted run;
- that `state_manager.py --run <id> status` shows progress at any time.

If they change the flow later, edit `flow.json`, re-validate, re-simulate and re-generate. Step files are never overwritten.

## Rules

- Keep one engine. Never hand-write a per-flow state manager or write state-handling code into the orchestrator; if the engine is missing a feature, say so instead.
- Gates exist for the human. Don't design flows where the agent approves its own gates.
- Prefer a few meaningful steps over many thin ones. Each step costs a subagent and a handoff.
