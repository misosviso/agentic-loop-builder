# Step: fix_ci

Part of the `development-loop` flow. You run as a subagent, and **only after CI failed** on the pull request. The `ci` wait step sends the run back here with the failing output. When you're done, the flow waits for CI again. This happens up to 3 times, and after that the user decides.

## Goal

Make the pull request's CI pass by fixing the actual cause, without hiding the failure.

## Inputs

- `ci.status.json`: the CI check output (`output` holds the failing lines). This is among your inputs, and the feedback names it.
- `requirements.md` and `pr.md`: what the change is for, and which pull request this is.
- The repository, checked out on the pull request's branch.

## Outputs

- A commit with the fix, pushed to the pull request's branch.
- `ci-fix.md`, with these headings:
  - `## Failure`: which check failed, the key error lines, and the root cause in one or two sentences.
  - `## Fix`: what you changed and why it fixes the cause. Name the files.
  - `## Commit`: the pushed commit hash.

The engine checks those headings, then checks that no tracked changes are left uncommitted and that nothing is left unpushed.

## Procedure

1. Read the failing output in `ci.status.json`. For more detail, run `gh pr checks` and `gh run view --log-failed`.
2. Reproduce the failure locally when you can, by running the same command CI runs.
3. Fix the cause in the code or the tests. Re-run the failing command until it passes, then run the touched modules' test suites.
4. Commit with the message `<story-key>: fix CI (<check name>)` and push with `git push`.
5. Write `ci-fix.md`.

## Done when

- [ ] The failing check's command passes locally, or you've explained why it can't run locally.
- [ ] The fix is committed and pushed, with nothing left uncommitted or unpushed.
- [ ] `ci-fix.md` names the root cause, not just the symptom.

## Constraints

- Never skip, disable or weaken a test or a check, and never edit the CI configuration to get green, unless the requirements say so.
- Never force-push or rewrite the branch's history.
- If the failure is unrelated to this change (flaky infrastructure, or broken on the base branch), don't patch around it. Say so in `ci-fix.md` and in your reply, and leave the code as it is. The user decides.
