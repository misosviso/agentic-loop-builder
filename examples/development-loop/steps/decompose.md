# Step: decomposition

Part of the `development-loop` flow. You run as a subagent. The orchestrator tells you which files to read, which paths to write, and any feedback from an earlier attempt.

## Goal

Split the approved requirements into small subtasks that can each be implemented and reviewed on their own. Each subtask gets a goal, its dependencies and verification points. The engine runs one implement/review cycle per subtask, in dependency order, and the user approves the plan at gate **G2**.

## Inputs

- `jira-context.md`
- `requirements.md`, already approved at G1
- The repository

## Outputs

- `subtasks.json`: `{"subtasks": [ ... ]}`. Each subtask has these fields:

  | Field | Required | Meaning |
  |---|---|---|
  | `id` | yes | `T1`, `T2`, … |
  | `title` | yes | Imperative and short, e.g. "Add retry policy to HttpClient" |
  | `goal` | yes | What is true when this subtask is done, in one or two sentences |
  | `description` | no | Approach, notable decisions, pitfalls |
  | `depends_on` | yes | Ids that must be done first; `[]` if none |
  | `verification` | yes, at least 1 | Concrete checks, e.g. a command to run and its expected result, or a test that must exist and pass |
  | `files` | no | Paths expected to change |
  | `requirements` | no | Which `R<n>` this subtask covers |

  The engine validates this file when you finish. It rejects unknown dependency ids, self-dependencies, dependency cycles and missing fields.
- `plan.md`: a readable version of the same plan for the user. Include a short overview, a table of subtasks (id, title, depends on, requirements covered), and a coverage check showing that every `R<n>` is covered by at least one subtask.

## Procedure

1. Read the requirements and the code areas they cite.
2. Slice the work vertically where you can. Each subtask should leave the code compiling and its tests passing. Aim for roughly 30 to 200 changed lines per subtask, and split anything bigger.
3. Make the dependencies honest. Add one only where a subtask truly needs another's output, so the engine can order the work correctly.
4. Write verification points a reviewer can execute: test names, commands and observable behavior. "Works correctly" is not a verification point.
5. Write `subtasks.json` first, then `plan.md` from it, so the two never disagree.
6. If feedback is given, revise both files and keep ids stable where the subtask still exists.

## Done when

- [ ] Every requirement is covered by at least one subtask.
- [ ] Every subtask has at least one executable verification point.
- [ ] Dependencies form no cycle and reference existing ids.
- [ ] `plan.md` and `subtasks.json` describe the same plan.

## Constraints

- Plan only; don't change code.
- Prefer fewer, meaningful subtasks over many trivial ones. A typical story has 2 to 8.
