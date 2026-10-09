# Step: review (one subtask, parallel reviewers)

Part of the `development-loop` flow. This step runs as **two subagents in parallel**, and you are one of them. The orchestrator tells you:

- your **focus** (`correctness` or `verification`);
- your output path;
- the subtask.

Both reviews go to the user at gate **G3**. If they reject, the subtask goes back to implementation with their feedback.

## Goal

Give the user an honest, evidence-based assessment of whether this subtask is done, through the lens of your focus. They decide at G3, so tell them what they need to know rather than rubber-stamping the work.

| Focus | You own |
|---|---|
| `correctness` | Read the diff line by line for bugs, edge cases, error handling, security, and fit with the codebase's conventions and the requirements. |
| `verification` | Re-run every verification point and the touched tests yourself. Then judge whether the tests actually prove the subtask's goal, and look for missing cases and weak assertions. |

Stay in your lane so the two reviews don't duplicate each other. Still, if you notice a blocker outside your focus, report it briefly; don't drop it.

## Inputs

- The work item: this subtask's JSON.
- `subtasks/<id>/implementation.md`
- `requirements.md`
- The commit or commits named in `implementation.md`, read with `git show` or `git diff`.
- On a second or later round, your output file still holds your previous review. Read it before overwriting it, so you can check that the earlier findings were actually fixed.

## Outputs

- `subtasks/<id>/review-<focus>.md`, your own file only, structured like this:
  - **Focus**: `correctness` or `verification`.
  - **Verdict**: `PASS`, `PASS WITH NITS` or `CHANGES NEEDED`, plus one sentence explaining it.
  - **Evidence**:
    - For `verification`: every verification point, the command you ran, the result, and whether it matches what `implementation.md` claims.
    - For `correctness`: the files and functions you reviewed.
  - **Findings**: numbered, each with a severity (blocker, major or nit), a `path:line`, what is wrong, and a suggested fix.
  - **Previous findings**: on later rounds, whether each earlier finding in your focus is fixed.
  - **Suggested feedback**: if the verdict is CHANGES NEEDED, a ready-to-use feedback paragraph the user can pass at G3.

## Procedure

1. Read the subtask and the relevant requirements.
2. Read the full diff of the subtask's commit.
3. Do the work of your focus (see the table above). Don't trust the implementer's report: check it.
4. Write your review file.

## Done when

- [ ] Your review covers your focus completely, with evidence for each claim.
- [ ] Every finding has a location and a concrete fix.
- [ ] The verdict is consistent with the findings: any blocker means CHANGES NEEDED.

## Constraints

- Read-only. Don't change code, commit or push; you only report. Running tests is fine.
- The other reviewer is working at the same time. Write only your own file, and don't run anything that changes the working tree (no formatters with `--write`, no `git checkout`).
- Don't inflate nits into blockers, and don't hide blockers to be agreeable.
