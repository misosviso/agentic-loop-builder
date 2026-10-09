# agentic-loop-builder

Build gated, resumable agent workflows ("loops") as Claude Code skills. For example:

> Jira story → requirements **(G1)** → subtasks **(G2)** → per subtask: implement → 2 reviewers in parallel **(G3, reject goes back to implement)** → **(G4)** open PR

You design the flow together with the agent. The builder then generates two things:

- a skill, run with `/development-loop PROJ-123`;
- an **orchestrator agent**, started with `claude --agent development-loop`.

The orchestrator only coordinates:

- **Steps run in subagents.** Each step runs in its own subagent, or in several at once where the flow says so, such as two reviewers with different focuses.
- **Gates stop for you.** Each gate stops and asks you to approve or reject.
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
  skills/development-loop/
    SKILL.md                                      # orchestrator as a skill (generated)
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

Then run it in either of two ways:

- **Dedicated orchestrator session:** `claude --agent development-loop`, then type the story key, e.g. `PROJ-123`.
- **From any session:** `/development-loop PROJ-123`.

The agent's tools are `Agent(general-purpose)`, Bash (for the engine), Read, Glob, Grep and AskUserQuestion, with no editing tools, so every step really is done by subagents. Run it as the main session rather than delegating to it, because only the main session can stop and ask you at gates. G4 is also enforced by a `PreToolUse` hook that blocks `gh pr create` until G4 is approved; the snippet for `.claude/settings.json` is at the end of the generated `SKILL.md`.

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

`generate` also writes `.claude/agents/my-loop.md`; change that with `--agent-path` or `--no-agent`. It always refreshes `state_manager.py` and `state.schema.json`. It keeps an existing `SKILL.md` and agent file unless you pass `--force`, and it never overwrites `steps/*.md`.

## Notes

- Everything is stdlib-only Python 3.9+, so there is nothing to install.
- Step skills live in `steps/*.md` rather than nested skill directories. Claude Code only discovers skills one level deep, and these are prompts the orchestrator hands to subagents, not standalone skills.
- Parallelism exists only *within* a step. Steps, and foreach items, still run one at a time, because implementation subagents share one working tree.
- OpenCode isn't targeted explicitly yet. The engine and step files are tool-agnostic, but the skill and hook locations differ, so check OpenCode's docs before wiring it up there.
