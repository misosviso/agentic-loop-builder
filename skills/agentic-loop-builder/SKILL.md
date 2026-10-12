---
name: agentic-loop-builder
description: "Design and generate a gated multi-step agent workflow: a /command, an orchestrator agent and per-step subagents."
argument-hint: "[loop-name] [short description]"
---

# Agentic Loop Builder

You help the user design a **loop**: a multi-step agent workflow with approval gates, for any domain. You then generate it as:

- **a command**, `/<loop-name> <run-id>`. Running it hands the run to the flow's orchestrator agent.
- **an orchestrator agent**, which only coordinates. It launches a subagent for each step (several in parallel where the flow says so) and stops at every gate. It can also run as its own session with `claude --agent <loop-name>`.
- **optional custom subagents** for steps that need a specialist. Steps without one use the generic `general-purpose` agent.

**The goal is a loop that works on its first real run.** A hand-written flow beats a naive generated one in four ways:

- It checks each output's *content*, not just that a file exists.
- It loops on its own until a condition holds, instead of only when a human rejects.
- It knows how to wait for things outside the loop, such as CI, a reviewer or a deployment.
- Its steps have the tools and access they need.

The engine supports all four. Your job in the interview is to make sure the flow uses them.

| Part | File | Role |
|---|---|---|
| Flow definition | `flow.json` | Steps, gates, contracts, loops, waits, requirements. Written by you and the user. |
| Run state | `.agentic-loops/<loop>/<run>/state.json` | One run's progress. Written **only** by the engine. |
| Engine | `state_manager.py` | Generic; reads `flow.json`, verifies outputs, evaluates conditions, polls waits, answers "what's next?". |

The tools live next to this file; `BUILDER` below means this file's directory:

- `scaffold.py`:
  - `check`: the readiness report;
  - `validate`;
  - `generate`;
  - `schema`.
- `state_manager.py`: the engine, copied into every generated loop. It also provides `simulate` and `preflight`.
- `templates/`: the orchestrator and step-stub templates.

## Process

Go phase by phase, and confirm each phase with the user before moving on. Phases 2 and 5 are where the quality comes from; don't rush them.

### 1. Understand the flow

Ask the user to describe the workflow in plain words: what goes in, what comes out, and where a human must say yes. If they gave a description as arguments, start from that. Establish:

- **Inputs.** What identifies a run, such as a ticket key, a URL or a document path?
- **The steps, roughly.** List each step and what it produces.
- **Repetition.** Does anything repeat per item (subtasks, files, services, documents)? What decides the items, and do they depend on each other?
- **Where humans decide.** Which outputs a human approves (`after` gates). Which actions need approval *before* they happen, such as pushing, deploying, sending, paying or deleting (`before` gates, hook-enforced where possible).

### 2. Design each step

Go through the steps one by one, and ask these questions about each. Write the answers down; they become `flow.json`. An empty answer is fine, but an unasked question is not.

1. **Output.** Which files does it write, and with what structure? Prefer Markdown with fixed headings for documents meant for humans, and JSON with a schema for anything a later step or the engine decides on.
2. **"How would an expert check this is right?"** Turn every answer you can into something the engine verifies at `complete`:
   - required headings (`sections`);
   - patterns that must appear (`contains`);
   - a JSON Schema (`json_schema`);
   - a minimum size;
   - commands that must exit 0 (`checks`), such as tests, a linter, `git status` being clean, a build, a link checker, or a schema validator.

   Output that fails is refused with the exact problems, the subagent fixes it, and after `max_fix_attempts` refusals the step fails to the user. **A step whose outputs only need to exist is a gap.** Close it unless there's truly nothing to check.
3. **"What happens when it isn't good enough?"** Choose who decides:
   - **A human:** an `after` gate. A rejection sends the work back to `on_reject`.
   - **An automatic condition** (`repeat`): after the step, a condition on its *structured* output decides whether the work loops back. For example, "a reviewer's JSON verdict is `CHANGES_NEEDED`", "the test report has failures", or "the critique scores below 4".
     - Ask what the condition is, where to go back to (`back_to`), and the most rounds (`max`).
     - Ask what happens when the rounds run out (`on_exhausted`): `fail`, `gate` (let the human decide) or `continue`.
   - **Both:** a cheap automatic loop first, then a human gate, which then only sees work that already passed the automatic checks.

   The data a condition reads must be written by a step with a JSON contract, so the engine can rely on it.
4. **"Does it only run sometimes?"** For example, only for bugs, only if migrations changed, or only when an earlier check failed. That's `when`: a condition on an input or an earlier output. When it's false, the step is skipped.
5. **"Does it have to wait for something outside the loop?"** For example, CI, a code review, a deployment, an approval in another system, a scheduled time, or a long-running job. That's a `wait` step. Ask:
   - **How to check it:**
     - a command, exit 0 when ready, listed `failed_exit_codes` when it definitely failed, anything else means not yet;
     - or a subagent that writes `{"status": "ready"|"pending"|"failed"}`, when checking needs MCP tools or judgement.
   - **How often to check** (`poll_every`), and **how long before giving up** (`timeout`, then `on_timeout`).
   - **What to do on failure** (`on_failure`): `fail`, or go `back_to` a fix step. That fix step usually sits just before the wait and runs only `when` the wait's status file says `failed`.
6. **"What does it need?"** Which commands (`gh`, `kubectl`, `jq`), environment variables (`API_TOKEN`, or `A|B` for either), MCP servers, and tools (Write, Bash, WebFetch, or an MCP tool). These become `requires`. `init` refuses to start a run if a command or variable is missing, and `check` verifies that the chosen subagent has the tools.
7. **Who does it?** Choose a subagent:
   - **Generic** (`general-purpose`): the default.
   - **Custom:** when the step needs different tools (read-only reviewer, writer, Jira fetcher with MCP), a different model (cheap `haiku` for fetching), or a persona (senior engineer in this codebase).
   - **Parallel agents** (`agents`): several subagents with different focuses writing different files, such as two reviewers.

   One custom subagent can serve several steps. To use an agent that already exists in `.claude/agents/`, reference it by name.

### 3. Confirm the step table

Present this table, and iterate until the user agrees:

| # | Step | Output (contract) | Verified by | If not good enough | Runs when | Waits on | Needs | Subagent | Gate |
|---|---|---|---|---|---|---|---|---|---|

Also list the custom subagents:

| Name | Used by | Good at | Tools | Model |
|---|---|---|---|---|

Call out anything risky:

- an irreversible action without a `before` gate;
- a loop without a cap or an exhaustion policy;
- a wait without a timeout;
- a decision based on free text instead of JSON;
- a step whose output is only checked for existence;
- a foreach whose items have no runnable verification points;
- parallel agents writing the same files;
- a subagent with more tools than its step needs.

### 4. Write `flow.json` and run the readiness check

Ask where the loop should live; the default is `.claude/skills/<name>/`. Write `flow.json` there, then run:

```
python3 BUILDER/scaffold.py check <loop-dir>/flow.json
```

The report says **READY** or **NOT READY**, and covers:

- **Structure:** unknown keys (with a "did you mean"), bad references, invalid conditions or durations.
- **Subagents:** whether each step's subagent can write its outputs, read its inputs, and run its required commands and MCP tools. A reviewer with only Read and Grep can't write its review file.
- **Environment:** the preflight of commands, variables and MCP servers. Use `--no-env` when checking on another machine.
- **Paths:** every branch simulated:
  - the happy path;
  - each gate rejected once and every time;
  - each automatic loop failing once and never passing;
  - each `when` false;
  - each wait pending, failing once and failing every time.

  A branch that never ends is an error. Show the user the paths, which document what their runs will do.
- **Gaps:** steps whose outputs are only checked for existence.

Fix the flow until it's READY. To see one branch in detail:

```
python3 BUILDER/state_manager.py --flow <loop-dir>/flow.json simulate --reject G1 --until review=1 --wait ci=2 --wait-fail ci=1 --skip fix_ci
```

### 5. Generate, then write the step skills together

```
python3 BUILDER/scaffold.py generate <loop-dir>/flow.json --out <loop-dir> [--install-path <path from project root>]
```

This writes:

- **`SKILL.md`, the `/<name>` command.** It uses `context: fork` with `agent: <name>`, so running the command starts the orchestrator agent.
  - The agent applies the decision passed in the arguments, if any.
  - It then advances the run to the next gate, failure, end, or long wait, and returns a report with the exact commands the user types next.
  - The command has `disable-model-invocation: true`, so only the user can run it, and Claude can't decide a gate on their behalf.
- **The orchestrator agent**, at `.claude/agents/<name>.md`. Change the path with `--agent-path`; with `--no-agent`, the command orchestrates inline instead.
  - Its tools are `Agent(<subagent types>)`, Bash for the engine, Read, Glob, Grep and AskUserQuestion. It can't edit files.
- **One `.claude/agents/<subagent>.md` per custom subagent**, with a stub prompt.
- **`state.schema.json`, `state_manager.py`, and a stub for every step skill.** Each stub has a **Checked automatically** section listing exactly what the engine will verify.

On regeneration:

- step files are kept;
- `SKILL.md` and the orchestrator agent are kept unless you pass `--force`;
- a subagent's frontmatter is re-synced from `flow.json`, and its prompt is kept.

If the output contains `hook_settings`, offer to merge it into `.claude/settings.json`, which hard-enforces guarded gates. Show the user the change first, and suggest adding `.agentic-loops/` to `.gitignore`.

Then fill in each stub with the user, one step at a time:

- **Outputs must promise exactly what the contract checks.** Use the same headings, JSON fields and enum values. A mismatch makes the first run fail on refusals.
- **Procedure:** which tools to use and how; where to get information; what to do when access fails. Have the subagent report the failure, not write a placeholder.
- **Done when:** criteria someone else can check. Turn every criterion you can into a contract or check in `flow.json`.
- **Constraints:** read-only steps, never pushing, never weakening tests, never skipping checks.
- **Wait steps checked by a subagent:** the subagent reports `pending` when something isn't ready yet, and never sleeps.
- **Custom subagents:** fill in the **Role** section of their prompts. It covers what they're expert at, the conventions they follow, and their hard limits. Make sure no subagent's limits contradict a step it runs; for example, an implementer that "never pushes" can't run a fix step that must push.

Put helper scripts that checks or waits call next to the flow, e.g. `<loop-dir>/scripts/`. Commands run from the project root, and they can use `{artifacts_dir}`, `{run}`, `{item}` and `{input.<name>}`.

For fully written examples, see `examples/development-loop/.claude/` in the agentic-loop-builder repository. It has contracts on every output, a "changes are committed" check, an automatic review loop on JSON verdicts before the human gate, and a CI wait with a conditional fix step.

### 6. Readiness again, and a first real run

Run `scaffold.py check` again; regenerating and editing can change the result. Then suggest a first real run on a small, low-risk input, and stay with the user for it:

- **Refusals:** each refusal shows a mismatch between a step skill and its contract. Fix whichever one is wrong.
- **Feedback:** watch whether the feedback that automatic loops pass back is actionable. If it isn't, sharpen the reviewer's output format.
- **History:** read `.agentic-loops/<loop>/<run>/history.jsonl` afterwards. It records every refusal, loop, poll and decision.

### 7. Hand over

Tell the user:

- **How to run it:** `/<name> <run-id>` advances the run. Each gate is answered with `/<name> <run-id> approve <gate> [note]` or `reject <gate> <feedback>`. Alternatively, `claude --agent <name>` runs an interactive session that asks at each gate.
- **Resuming:** the same command resumes an interrupted or waiting run. Long waits stop the command; re-run it later.
- **Inspecting:** where runs live, and that `state_manager.py --run <id> status` shows progress at any time.
- **Changing the flow:** edit `flow.json`, run `check`, then `generate`. Step files are never overwritten.

## Flow definition reference

```jsonc
{
  "name": "development-loop",              // kebab-case; becomes /development-loop
  "description": "…one short sentence…",   // the command's description, under 200 characters
  "inputs": { "story": "Jira key" },       // required at `init --input story=…`
  "defaults": { "run_in_subagent": true, "max_iterations": 3, "max_fix_attempts": 3,
                "subagent_type": "general-purpose" },
  "requires": { "commands": ["git"] },     // the whole flow's needs; steps add their own
  "orchestrator": { "model": "inherit", "color": "blue" },
  "subagents": { "development-loop-implementer": {
      "description": "Implements one coding subtask with tests and commits it.",
      "tools": ["Read", "Write", "Edit", "Glob", "Grep", "Bash"] } },  // + model, disallowedTools, mcpServers…
  "schemas": { "subtask": { … }, "review_verdict": { … } },             // named JSON Schemas
  "steps": [
    { "id": "requirements", "kind": "single", "skill": "steps/requirements.md",
      "needs": ["jira_context"],                    // earlier steps; rerun with it when sent back
      "inputs": ["jira_context"],                   // whose artifacts it reads (default: needs)
      "artifacts": [{ "path": "requirements.md",    // a contract; a plain string only needs to exist
                      "sections": ["## Scope", "## Functional requirements"],
                      "contains": ["\\bR1\\b"], "min_bytes": 200 }],
      "gate": { "id": "G1", "when": "after", "prompt": "…", "on_reject": "requirements" } },
    { "id": "decomposition", "kind": "single", "skill": "…", "artifacts": ["subtasks.json"],
      "produces_items": { "file": "subtasks.json", "key": "subtasks", "item_schema": "subtask" } },
    { "id": "subtasks", "kind": "foreach", "over": "decomposition", "respect_item_dependencies": true,
      "cycle": [
        { "id": "implement", "skill": "…", "subagent_type": "development-loop-implementer",
          "artifacts": [{ "path": "subtasks/{item}/implementation.md", "sections": ["## Commit"] }],
          "checks": [{ "name": "changes are committed", "run": "test -z \"$(git status --porcelain)\"" }],
          "requires": { "commands": ["git"] } },
        { "id": "review", "skill": "steps/review.md",
          "agents": [{ "id": "correctness", "focus": "…" }, { "id": "verification", "focus": "…" }],
          "artifacts": [{ "path": "subtasks/{item}/review-{agent}.json", "json_schema": "review_verdict" }],
          "repeat": { "until": { "all": [
                        { "file": "subtasks/{item}/review-correctness.json", "field": "verdict", "not_equals": "CHANGES_NEEDED" },
                        { "file": "subtasks/{item}/review-verification.json", "field": "verdict", "not_equals": "CHANGES_NEEDED" } ] },
                      "back_to": "implement", "max": 3, "on_exhausted": "gate" },
          "gate": { "id": "G3", "on_reject": "implement" } } ] },
    { "id": "open_pr", "kind": "single", "skill": "…", "needs": ["subtasks"],
      "requires": { "commands": ["gh"] },
      "gate": { "id": "G4", "when": "before", "guard": { "tool": "Bash", "pattern": "gh\\s+pr\\s+create" } } },
    { "id": "fix_ci", "kind": "single", "skill": "steps/fix-ci.md",
      "when": { "file": "ci.status.json", "field": "status", "equals": "failed" }, "artifacts": ["…"] },
    { "id": "ci", "kind": "single",
      "wait": { "check": { "run": "sh scripts/ci_status.sh", "failed_exit_codes": [1] },  // or "check": "agent"
                "poll_every": "3m", "timeout": "2h",
                "on_failure": { "back_to": "fix_ci" }, "on_timeout": "fail" } }
  ]
}
```

How the engine behaves, which you should explain to the user as it becomes relevant:

- **Order.** Steps run one at a time, in listed order. `needs` and `inputs` reference earlier steps only. Unknown keys are errors, with a "did you mean".
- **Contracts and checks.** At `complete`, every artifact is verified:
  - `sections`: lines starting with these headings;
  - `contains`: regexes;
  - `min_bytes`;
  - `json_schema`: a schema object or a name in `schemas`;
  - `optional`;
  - then the step's `checks` run from the project root and must exit 0.

  Failing output is refused with the problems listed. After `max_fix_attempts` refusals the step (or parallel agent) fails. A step that `produces_items` has its items validated too.
- **Conditions** (`when`, `repeat.until`) are declarative:
  - `{"file": "x.json", "field": "a.b", "equals": …}`: without `field`, the test applies to the file's text;
  - `{"input": "kind", "in": [...]}`;
  - combined with `{"all": [...]}`, `{"any": [...]}` and `{"not": …}`;
  - tests: `equals`, `not_equals`, `in`, `not_in`, `matches`, `gte`, `lte`, `exists`.

  A missing file or field fails every test except `exists`. `{item}` in paths becomes the current item.
- **`when`.** Evaluated when the step comes up. If false, the step is `skipped`, which counts as done. If it is sent back to later, it is evaluated again.
- **`repeat`.** Evaluated after the step's output is accepted. If the condition is false, the work goes back to `back_to` (default: the step itself), with feedback naming the condition and the step's files as inputs. This repeats up to `max` rounds; then `on_exhausted` applies. A human rejection gives the loop a fresh budget.
- **Gates.** An `after` gate approves an output, and a rejection sends it back to `on_reject`. A `before` gate approves an action before it happens; rejecting one without `on_reject` halts the run. `max_iterations` caps the attempts at a send-back target. `guard` adds a hook that blocks the guarded tool call unless that step is current.
- **`wait`.**
  - When the step comes up, it starts waiting.
  - A command check runs on `poll`. A subagent check comes back as a `run_step` when due, and its subagent writes the status file.
  - The status is written to `<step>.status.json` (or `wait.status_file`), so later conditions and fix steps can read it.
  - `ready` finishes the step, which then goes through `repeat` and its gate as usual.
  - `pending` schedules the next check after `poll_every`.
  - `failed` applies `on_failure`, and passing `timeout` applies `on_timeout`.
  - The orchestrator polls, blocking for up to about 10 minutes, or stops and tells the user when the next check is due.
- **`requires`.** Commands on PATH, environment variables (`A|B` means either one), MCP servers and tools. `init` runs a preflight and refuses to start if anything is missing, unless `--skip-preflight` is given. `check` also verifies the subagents' tools.
- **`foreach` / `produces_items`.** The items file is validated (schema, unique ids, existing dependencies, no cycles) and expanded into one cycle per item.
- **`agents`.** Parallel subagents in one step, each with its own `focus`, outputs (`{agent}` in paths) and, optionally, its own `skill` and `subagent_type`. The step finishes after every agent reports. `retry` reruns only the agents that didn't finish.
- **`subagents`.** Generated into `.claude/agents/<name>.md`. Prefix their names with the flow name to avoid clashes.

## Rules

- Keep one engine. Never hand-write a per-flow state manager or put state logic in the orchestrator. If the engine is missing a feature, say so.
- Gates exist for the human. Don't design flows where an agent approves its own gates.
- Every automatic decision reads structured output that has a contract. Never route on prose.
- Every loop has a cap and an exhaustion policy. Every wait has a timeout.
- Prefer a few meaningful steps over many thin ones. Each step costs a subagent and a handoff.
