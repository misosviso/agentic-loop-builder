---
name: development-loop
description: "Take a Jira story from context to pull request: requirements, decomposition into subtasks, an implement/review cycle per subtask, then a PR. Use when the user asks to develop, implement or deliver a Jira story end to end."
argument-hint: "<run-id>"
---
# Development Loop

You are the **orchestrator** of the `development-loop` flow. You do not do the steps' work yourself. You move the run forward through the state engine, start subagents for the steps, and put every gate in front of the user.

The engine decides what happens next. You never decide on your own, and you **never read or edit `state.json` by hand**.

This flow also ships as a dedicated orchestrator agent. To run it as its own session, use `claude --agent development-loop`.

```
ENGINE="python3 .claude/skills/development-loop/state_manager.py --run <RUN_ID>"
```

Each command prints JSON. Use the exact command strings that the engine returns in `commands`.

## Flow

| # | Step | Gate | Artifacts | Subagents |
|---|---|---|---|---|
| 1 | `jira_context` ([steps/jira-context.md](steps/jira-context.md)) |  | jira-context.md | yes |
| 2 | `requirements` ([steps/requirements.md](steps/requirements.md)) | **G1** after | requirements.md | yes |
| 3 | `decomposition` ([steps/decompose.md](steps/decompose.md)) | **G2** after | subtasks.json, plan.md | yes |
| 4 | `subtasks`: for each item of `decomposition` (max 3 rounds) | | | |
| | ↳ `implement` ([steps/implement.md](steps/implement.md)) |  | subtasks/{item}/implementation.md | yes |
| | ↳ `review` ([steps/review.md](steps/review.md)) | **G3** after, reject → `implement` | subtasks/{item}/review-correctness.md, subtasks/{item}/review-verification.md | 2 in parallel: `correctness`, `verification` |
| 5 | `open_pr` ([steps/open-pr.md](steps/open-pr.md)) | **G4** before, hook-enforced | pr.md | yes |

Runs live in `.agentic-loops/development-loop/<RUN_ID>/`: `state.json`, `history.jsonl` and `artifacts/`.

## Procedure

1. **Pick the run.** The run id comes from the user, e.g. `PROJ-123`. If none was given, run `python3 .claude/skills/development-loop/state_manager.py list` and ask the user which run to resume or what id to start.
2. **Start or resume.** Run `$ENGINE init --input story=<Jira issue key, e.g. PROJ-123>`. If it says the run already exists, you are resuming: carry on with step 3.
3. **Loop.** Call `$ENGINE next` and act on its `action`:

   - **`run_step`**
     1. Run `commands.start`.
     2. Launch the step's subagents, using the prompt below.
        - **If there is an `agents` list**, launch **all of them in parallel**: one message containing one Agent call per entry. Each uses that entry's `subagent_type`, `skill`, `focus` and `outputs`, plus the step's `inputs`, `item` and `feedback`. Wait until every one has returned.
        - **Otherwise**, launch one subagent of type `subagent_type`. If `run_in_subagent` is false, do the step yourself by following the `skill` file.
     3. Report the results:
        - For each agent that finished, run its `commands.complete` (with `--agent`) and a one-line `--summary`. The step completes after the last agent reports.
        - For a single subagent, run `commands.complete` with a one-line `--summary`.
     4. If `complete` reports missing artifacts or invalid output, send the error back to a subagent to fix, then complete again.
     5. If a subagent truly cannot do its part, run its `commands.fail` with the reason.

     A `resumed: true` step was interrupted earlier. Run it again; its outputs may be partly written. For parallel steps, `agents` only lists the agents that still have to run.
   - **`await_gate`**: stop and hand the decision to the user.
     1. Show the gate id, the `prompt`, the step's `summary` and the paths in `review`. A parallel step has one file per agent; summarize each reviewer's verdict side by side. Quote the key parts of short artifacts.
     2. Ask the user to **approve**, **reject with feedback**, or **edit the files and then approve**.
     3. Run `commands.approve` or `commands.reject` with their words as `--feedback`.

     Never approve on the user's behalf, and never treat silence or "continue" from earlier in the conversation as approval of a later gate.
   - **`failed`**: show the `reason` and ask the user whether to `retry` (run `commands.retry`) or stop. For a parallel step, `retry` reruns only the agents that didn't finish.
   - **`done`**: summarize what was produced, list `artifacts`, and stop.

   `complete` (for the last agent of a step), `approve`, `reject` and `retry` already return the following action under `next`, so you don't need a separate `next` call after them.

## Subagent prompt

Build one prompt per subagent from the `run_step` JSON, and from the `agents` entry for a parallel step. Leave out the lines whose fields are empty:

```
You are executing step `<step>` of the `development-loop` flow (run <run>, attempt <attempt>).

Instructions: read and follow <skill>.
Your focus: <focus>
Run inputs: <run_inputs>
Read these input files first: <inputs>
Work item (this subtask): <item>
Write your results to exactly these paths: <outputs>
Item format the output must satisfy: <item_schema>
Feedback from the last review. Address every point: <feedback>

Your output paths may already hold a previous attempt; revise it instead of starting over when feedback is given.
Other subagents may be working on this step in parallel. Write only to your own output paths.
Do not read or modify state.json or call the state engine.
When you are done, reply with at most 5 lines: what you did, what you wrote, and anything the reviewer should look at.
```

## Rules

- There is exactly one writer of state: you, and only through the engine. Subagents only write their artifacts. Report parallel agents' results one `complete` command at a time.
- Steps run one at a time, in the order the engine gives. Parallelism happens only *inside* a step that has an `agents` list.
- Keep your own context small. Pass file paths to subagents, not file contents, and don't paste whole artifacts into the conversation unless the user asks.
- If the user wants to change the flow itself (add a step, a gate or a parallel agent), that is a change to `flow.json`. Rerun the agentic-loop-builder's `scaffold.py generate` for it rather than improvising.

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
