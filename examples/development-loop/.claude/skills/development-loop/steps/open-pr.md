# Step: open_pr

Part of the `development-loop` flow. You run as a subagent. The user approved gate **G4** *before* this step, which is the approval to push and open the PR. A `PreToolUse` hook blocks `gh pr create` unless that approval is in place.

## Goal

Push the feature branch and open one pull request that a reviewer can understand without having followed the loop.

## Inputs

- `jira-context.md` and `requirements.md`
- Every subtask's `implementation.md` and its two reviews (`review-correctness.md`, `review-verification.md`)
- The feature branch with one commit per subtask attempt

## Outputs

- A pushed branch and an open pull request.
- `pr.md`: the PR URL, the title, the body exactly as submitted, and the branch name.

## Procedure

1. Check the branch:
   - the working tree is clean;
   - the branch is up to date with the default branch (merge it in if needed, and re-run the tests if anything changed);
   - the full test suite passes.
2. Write the PR body:
   - **Summary**: what and why, with a link to the Jira issue.
   - **Changes**: one bullet per subtask, giving its title and what it changed.
   - **Requirements coverage**: each `R<n>` and where it is implemented and tested.
   - **Verification**: how it was tested, plus the commands a reviewer can run.
   - **Notes for reviewers**: decisions and open questions from the reviews.

   If the repository has a PR template, follow its structure instead.
3. Run `git push -u origin <branch>`, then `gh pr create --title "<KEY>: <summary>" --body-file <tmp file>`. Use the repository's default base branch unless the requirements say otherwise.
4. Write `pr.md`.

## Done when

- [ ] The PR exists and its URL is recorded in `pr.md`.
- [ ] The PR body covers every requirement and links the Jira issue.
- [ ] The suite was green on the pushed head.

## Constraints

- Never force-push and never merge the PR.
- If the hook blocks `gh pr create`, stop and report it. The gate is not approved, so don't try to work around it.
