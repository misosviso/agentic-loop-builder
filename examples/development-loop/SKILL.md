---
name: development-loop
description: Take a Jira story from context to pull request: requirements, decomposition into subtasks, an implement/review cycle per subtask, then a PR. Use when the user asks to develop, implement or deliver a Jira story end to end.
argument-hint: <run-id>
---

# Development Loop

You are the **orchestrator** of the `development-loop` flow. You do not do the steps' work yourself. You move the run forward through the state engine, start a subagent for each step, and put every gate in front of the user.

The engine decides what happens next. You never decide on your own, and you **never read or edit `state.json` by hand**.

```
ENGINE="python3 .claude/skills/development-loop/state_manager.py --run <RUN_ID>"
```

Each command prints JSON. Use the exact command strings that the engine returns in `commands`.

## Flow

| # | Step | Gate | Artifacts | Subagent |
|---|---|---|---|---|
| 1 | `jira_context` ([steps/jira-context.md](steps/jira-context.md)) |  | jira-context.md | yes |
| 2 | `requirements` ([steps/requirements.md](steps/requirements.md)) | **G1** after | requirements.md | yes |
| 3 | `decomposition` ([steps/decompose.md](steps/decompose.md)) | **G2** after | subtasks.json, plan.md | yes |
| 4 | `subtasks`: for each item of `decomposition` (max 3 rounds) | | | |
| | ↳ `implement` ([steps/implement.md](steps/implement.md)) |  | subtasks/{item}/implementation.md | yes |
| | ↳ `review` ([steps/review.md](steps/review.md)) | **G3** after, reject → `implement` | subtasks/{item}/review.md | yes |
| 5 | `open_pr` ([steps/open-pr.md](steps/open-pr.md)) | **G4** before, hook-enforced | pr.md | yes |

Runs live in `.agentic-loops/development-loop/<RUN_ID>/`: `state.json`, `history.jsonl` and `artifacts/`.

## Procedure

1. **Pick the run.** The run id is the argument, e.g. `/development-loop PROJ-123`. If no argument was given, run `python3 .claude/skills/development-loop/state_manager.py list` and ask the user which run to resume or what id to start.
2. **Start or resume.** Run `$ENGINE init --input story=<Jira issue key, e.g. PROJ-123>`. If it says the run already exists, you are resuming: carry on with step 3.
3. **Loop.** Call `$ENGINE next` and act on its `action`:

   - **`run_step`**
     1. Run `commands.start`.
     2. If `run_in_subagent` is true, spawn **one** subagent with the prompt below. If it is false, do the step yourself by following the `skill` file.
     3. When the work comes back, run `commands.complete` with a one-line `--summary`.
     4. If `complete` reports missing artifacts or invalid output, send the error back to a subagent to fix, then complete again.
     5. If the step truly cannot be done, run `commands.fail` with the reason.

     A `resumed: true` step was interrupted earlier. Run it again; its outputs may be partly written.
   - **`await_gate`**: stop and hand the decision to the user.
     1. Show the gate id, the `prompt`, the step's `summary` and the paths in `review`. Quote the key parts of short artifacts.
     2. Ask the user to **approve**, **reject with feedback**, or **edit the files and then approve**.
     3. Run `commands.approve` or `commands.reject` with their words as `--feedback`.

     Never approve on the user's behalf, and never treat silence or "continue" from earlier in the conversation as approval of a later gate.
   - **`failed`**: show the `reason` and ask the user whether to `retry` (run `commands.retry`) or stop.
   - **`done`**: summarize what was produced, list `artifacts`, and stop.

   `complete`, `approve`, `reject` and `retry` already return the following action under `next`, so you don't need a separate `next` call after them.

## Subagent prompt

Build it from the `run_step` JSON. Leave out the lines whose fields are empty:

```
You are executing step `<step>` of the `development-loop` flow (run <run>, attempt <attempt>).

Instructions: read and follow <skill>.
Run inputs: <run_inputs>
Read these input files first: <inputs>
Work item (this subtask): <item>
Write your results to exactly these paths: <outputs>
Item format the output must satisfy: <item_schema>
Feedback from the last review. Address every point: <feedback>

Your output paths may already hold a previous attempt; revise it instead of starting over when feedback is given.
Do not read or modify state.json or call the state engine.
When you are done, reply with at most 5 lines: what you did, what you wrote, and anything the reviewer should look at.
```

## Rules

- There is exactly one writer of state: you, and only through the engine.
- Steps run one at a time in the order the engine gives. Don't skip ahead or run steps in parallel.
- Keep your own context small. Pass file paths to subagents, not file contents, and don't paste whole artifacts into the conversation unless the user asks.
- If the user wants to change the flow itself (add a step or a gate), that is a change to `flow.json`. Rerun the agentic-loop-builder's `scaffold.py generate` for it rather than improvising.

## Hard gates

These gates are also enforced by a `PreToolUse` hook. The matching tool call is blocked unless the gated step is the run's current step:

- **G4** before `open_pr`: Bash calls matching `gh\s+pr\s+create`

If the hook blocks you, the gate hasn't been approved. Go back to the loop and don't work around it.
The hook is installed with this snippet in `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR\"/.claude/skills/development-loop/state_manager.py --runs-dir \"$CLAUDE_PROJECT_DIR\"/.agentic-loops guard"
          }
        ]
      }
    ]
  }
}
```
