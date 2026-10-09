# agentic-loop-builder

Build gated, resumable agent workflows ("loops") as Claude Code skills. For example:

> Jira story → requirements **(G1)** → subtasks **(G2)** → per subtask: implement → 2 reviewers in parallel **(G3, reject goes back to implement)** → **(G4)** open PR

You design the flow together with the agent. The builder then generates:

- **a command**, `/development-loop PROJ-123`, which hands the run straight to the flow's orchestrator agent;
- **an orchestrator agent**, which only coordinates (described below);
- **optional custom subagents** for steps that need a specialist, such as an implementer that writes code or a Jira fetcher. Steps without one use the generic `general-purpose` agent.

The orchestrator agent works like this:

- **Steps run in subagents.** Each step runs in its own subagent, or in several at once where the flow says so, such as two reviewers with different focuses.
- **Gates stop for you.** The run stops at each gate and waits for you to approve or reject.
- **Runs resume.** A small state engine keeps track of where every run is, so an interrupted run resumes where it left off.

## Layout

```
skills/agentic-loop-builder/      # the builder skill: install this one
  SKILL.md                        # interview: describe → step table → flow.json → generate → write steps
  scaffold.py                     # flow.json → orchestrator SKILL.md, state.schema.json, step stubs
  state_manager.py                # the generic state engine (copied into every generated loop)
  templates/                      # orchestrator + step-stub templates

examples/development-loop/.claude/                # a generated loop with hand-written step skills
  agents/development-loop.md                      # orchestrator agent (generated)
  agents/development-loop-jira.md                 # custom subagent: fetches the Jira story
  agents/development-loop-implementer.md          # custom subagent: writes the code
  skills/development-loop/
    SKILL.md                                      # the /development-loop command; forks into the agent (generated)
    flow.json                                     # the flow definition
    state.schema.json                             # JSON Schema of this flow's state.json (generated)
    state_manager.py                              # engine copy (generated)
    steps/{jira-context,requirements,decompose,implement,review,open-pr}.md

tests/                            # python3 -m unittest discover -s tests
```

## Three separate parts

| Part | Where | Written by |
|---|---|---|
| **Flow definition** | `flow.json` | You and the builder, once per flow type |
| **Run state** | `.agentic-loops/<flow>/<run>/state.json`, `history.jsonl`, `artifacts/` | Only `state_manager.py`, one run per story |
| **Engine** | `state_manager.py` | Generic and tested; the same file in every loop |

The orchestrator, whether the skill or the agent, is thin. It asks the engine `next`, then does what the engine says:

```jsonc
{"action": "run_step",   "step": "subtasks[T2].implement", "skill": ".../steps/implement.md",
 "subagent_type": "general-purpose", "inputs": [...], "outputs": [...],
 "feedback": "Gate G3 rejected: ...", "attempt": 2, "item": {...}}
{"action": "run_step",   "step": "subtasks[T2].review", "inputs": [...],
 "agents": [{"agent": "correctness",  "focus": "...", "outputs": [".../review-correctness.md"], ...},
            {"agent": "verification", "focus": "...", "outputs": [".../review-verification.md"], ...}]}
{"action": "await_gate", "gate": "G3", "when": "after", "review": [...], "prompt": "..."}
{"action": "failed",     "step": "...", "reason": "still rejected after 3 attempt(s) ..."}
{"action": "done",       "artifacts": [...]}
```

The engine owns all the logic:

- the order of steps;
- `foreach` expansion over items an earlier step produced, validated against an item schema, with dependency ordering and cycle detection;
- sending rejected work back with feedback, and rerunning what depends on it;
- `max_iterations` caps;
- parallel `agents` within a step: all start together, each reports with `complete --agent`, and the step finishes once the last one reports. A failed agent fails the step, and `retry` reruns only the agents that didn't finish;
- `after` gates (approve an output) and `before` gates (approve an action);
- atomic writes, a history log and resume.

## Use it

Install the builder into a project (or into `~/.claude/skills/` for all projects):

```bash
cp -r skills/agentic-loop-builder <project>/.claude/skills/
```

Then, in Claude Code, run `/agentic-loop-builder` and describe your flow. Or start from the example:

```bash
mkdir -p <project>/.claude && cp -r examples/development-loop/.claude/. <project>/.claude/
echo ".agentic-loops/" >> <project>/.gitignore
```

Then run it:

```
/development-loop PROJ-123                                   # runs until the first gate, then reports
/development-loop PROJ-123 approve G1 looks good             # records your decision, runs to the next gate
/development-loop PROJ-123 reject G3 handle the empty list   # sends the subtask back to implement
```

**How the command works:**

- **It runs the agent.** The command uses `context: fork` with `agent: development-loop`, so each invocation runs the orchestrator agent. The agent advances the run until the next gate, then hands you a report with the files to review and the exact commands to type next.
- **Only you decide gates.** The command has `disable-model-invocation: true`, so only you can run it, and Claude can't record a gate decision for you.
- **Interactive alternative:** run `claude --agent development-loop` and give it the story key. The agent then asks at each gate in the conversation.

**The agent's tools:**

- `Agent(development-loop-jira, general-purpose, development-loop-implementer)`, Bash for the engine, Read, Glob, Grep and AskUserQuestion. It has no editing tools, so every step really is done by subagents.
- G4 is also enforced by a `PreToolUse` hook that blocks `gh pr create` until G4 is approved. The snippet for `.claude/settings.json` is at the end of the agent file.

**Custom subagents.** The flow defines them in `flow.json` under `subagents`, and steps pick one with `subagent_type`:

- **Where they're written:** each becomes `.claude/agents/<name>.md`.
- **What regeneration does:** the frontmatter (description, tools, model, …) is re-synced from `flow.json`, and the prompt body you wrote is kept.
- **In the example:** `development-loop-jira` runs on `haiku` and can't edit code; `development-loop-implementer` has write tools. Requirements, decomposition, the reviewers and the PR step use the generic `general-purpose` agent.

### Engine CLI

```bash
E="python3 .claude/skills/development-loop/state_manager.py --run PROJ-123"
$E init --input story=PROJ-123
$E next                                   # the single next action, as JSON
$E start 'subtasks[T1].implement'
$E complete 'subtasks[T1].implement' --summary "added retry policy"
$E complete 'subtasks[T1].review' --agent correctness --summary "2 nits"   # one per parallel agent
$E approve G3 --note "nice"
$E reject G3 --feedback "handle the empty list"
$E retry 'subtasks[T1].implement'         # after max_iterations, or after a failed step
$E status                                 # human-readable progress
python3 .claude/skills/development-loop/state_manager.py simulate --reject G3   # dry run of the flow
```

### Scaffolder

```bash
python3 skills/agentic-loop-builder/scaffold.py validate my-loop/flow.json
python3 skills/agentic-loop-builder/scaffold.py generate .claude/skills/my-loop/flow.json --out .claude/skills/my-loop
```

Besides the skill directory, `generate` also writes:

- `.claude/agents/my-loop.md`, the orchestrator agent. Change its location with `--agent-path`. With `--no-agent`, the command orchestrates inline instead of forking.
- one `.claude/agents/<name>.md` per custom subagent.

On regeneration:

- `state_manager.py` and `state.schema.json` are always refreshed;
- an existing `SKILL.md` and orchestrator agent are kept unless you pass `--force`;
- custom subagents get their frontmatter synced, and their prompts are kept;
- `steps/*.md` are never overwritten.

## Notes

- Everything is stdlib-only Python 3.9+, so there is nothing to install.
- Step skills live in `steps/*.md` rather than nested skill directories. Claude Code only discovers skills one level deep, and these are prompts the orchestrator hands to subagents, not standalone skills.
- Parallelism exists only *within* a step. Steps, and foreach items, still run one at a time, because implementation subagents share one working tree.
- OpenCode isn't targeted explicitly yet. The engine and step files are tool-agnostic, but the skill and hook locations differ, so check OpenCode's docs before wiring it up there.
