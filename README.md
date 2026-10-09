# agentic-loop-builder

Build gated, resumable agent workflows ("loops") as Claude Code skills. For example:

> Jira story → requirements **(G1)** → subtasks **(G2)** → per subtask: implement → review **(G3, reject goes back to implement)** → **(G4)** open PR

You design the flow together with the agent. The builder then generates a skill you run with `/development-loop PROJ-123`. Each step runs in its own subagent. Each gate stops and asks you. A small state engine keeps track of where every run is, so an interrupted run resumes where it left off.

## Layout

```
skills/agentic-loop-builder/      # the builder skill: install this one
  SKILL.md                        # interview: describe → step table → flow.json → generate → write steps
  scaffold.py                     # flow.json → orchestrator SKILL.md, state.schema.json, step stubs
  state_manager.py                # the generic state engine (copied into every generated loop)
  templates/                      # orchestrator + step-stub templates

examples/development-loop/        # a generated loop with hand-written step skills
  SKILL.md                        # orchestrator (generated)
  flow.json                       # the flow definition
  state.schema.json               # JSON Schema of this flow's state.json (generated)
  state_manager.py                # engine copy (generated)
  steps/{jira-context,requirements,decompose,implement,review,open-pr}.md

tests/                            # python3 -m unittest discover -s tests
```

## Three separate parts

| Part | Where | Written by |
|---|---|---|
| **Flow definition** | `flow.json` | You and the builder, once per flow type |
| **Run state** | `.agentic-loops/<flow>/<run>/state.json`, `history.jsonl`, `artifacts/` | Only `state_manager.py`, one run per story |
| **Engine** | `state_manager.py` | Generic and tested; the same file in every loop |

The orchestrator skill is thin. It asks the engine `next`, then does what the engine says:

```jsonc
{"action": "run_step",   "step": "subtasks[T2].implement", "skill": ".../steps/implement.md",
 "inputs": [...], "outputs": [...], "feedback": "Gate G3 rejected: ...", "attempt": 2, "item": {...}}
{"action": "await_gate", "gate": "G3", "when": "after", "review": [...], "prompt": "..."}
{"action": "failed",     "step": "...", "reason": "still rejected after 3 attempt(s) ..."}
{"action": "done",       "artifacts": [...]}
```

The engine owns all the logic:

- the order of steps;
- `foreach` expansion over items an earlier step produced, validated against an item schema, with dependency ordering and cycle detection;
- sending rejected work back with feedback, and rerunning what depends on it;
- `max_iterations` caps;
- `after` gates (approve an output) and `before` gates (approve an action);
- atomic writes, a history log and resume.

## Use it

Install the builder into a project (or into `~/.claude/skills/` for all projects):

```bash
cp -r skills/agentic-loop-builder <project>/.claude/skills/
```

Then, in Claude Code, run `/agentic-loop-builder` and describe your flow. Or start from the example:

```bash
cp -r examples/development-loop <project>/.claude/skills/
echo ".agentic-loops/" >> <project>/.gitignore
```

Then run `/development-loop PROJ-123`. G4 is also enforced by a `PreToolUse` hook that blocks `gh pr create` until G4 is approved; the snippet for `.claude/settings.json` is at the end of the generated `SKILL.md`.

### Engine CLI

```bash
E="python3 .claude/skills/development-loop/state_manager.py --run PROJ-123"
$E init --input story=PROJ-123
$E next                                   # the single next action, as JSON
$E start 'subtasks[T1].implement'
$E complete 'subtasks[T1].implement' --summary "added retry policy"
$E approve G3 --note "nice"
$E reject G3 --feedback "handle the empty list"
$E retry 'subtasks[T1].implement'         # after max_iterations, or after a failed step
$E status                                 # human-readable progress
python3 .claude/skills/development-loop/state_manager.py simulate --reject G3   # dry run of the flow
```

### Scaffolder

```bash
python3 skills/agentic-loop-builder/scaffold.py validate my-loop/flow.json
python3 skills/agentic-loop-builder/scaffold.py generate my-loop/flow.json --out .claude/skills/my-loop
```

`generate` always refreshes `state_manager.py` and `state.schema.json`. It keeps an existing `SKILL.md` (unless you pass `--force`) and never overwrites `steps/*.md`.

## Notes

- Everything is stdlib-only Python 3.9+, so there is nothing to install.
- Step skills live in `steps/*.md` rather than nested skill directories. Claude Code only discovers skills one level deep, and these are prompts the orchestrator hands to subagents, not standalone skills.
- OpenCode isn't targeted explicitly yet. The engine and step files are tool-agnostic, but the skill and hook locations differ, so check OpenCode's docs before wiring it up there.
