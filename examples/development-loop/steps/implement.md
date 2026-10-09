# Step: implement (one subtask)

Part of the `development-loop` flow. You run as a subagent for **one** subtask. The orchestrator gives you the subtask, the input files, your output path, and the reviewer's feedback when this is a retry.

## Goal

Implement exactly this subtask so that all of its verification points pass, without breaking anything else.

## Inputs

- The work item: this subtask's JSON, with `goal`, `verification`, `depends_on` and `files`.
- `requirements.md`, `plan.md` and `subtasks.json`, for context. Implement only *this* subtask.
- On a retry: the previous `review.md` for this subtask and the feedback from gate G3. Every point in them must be addressed.

## Outputs

- Code changes in the working tree, on the story's feature branch.
- `subtasks/<id>/implementation.md`, containing:
  - **Changes**: the files touched, with one line on each.
  - **Verification**: each verification point from the item, the exact command or test you ran, and the result, either pass with key output or fail and why.
  - **Decisions**: anything you chose that a reviewer might question.
  - **Feedback addressed**: on a retry, each feedback point and how you handled it.
  - **Commit**: the commit hash.

## Procedure

1. Make sure you are on the feature branch. If none exists yet, create `feature/<story-key>-<short-slug>` from the default branch.
2. Read the code you will touch, and the tests near it. Follow the existing patterns.
3. Write or extend tests for the verification points first, where that makes sense.
4. Implement the change, keeping the diff focused on this subtask.
5. Run every verification point, plus the test suites of the modules you touched. Run the project's linter or formatter if it has one.
6. Commit with the message `<story-key>: <subtask title> (<id>)`. Don't push.
7. Write `implementation.md`.

## Done when

- [ ] Every verification point was actually run and passes, with the evidence recorded.
- [ ] Existing tests in the touched areas still pass.
- [ ] The diff contains nothing unrelated to this subtask.
- [ ] One commit exists for this attempt, recorded in `implementation.md`.

## Constraints

- Never push, open a PR, or rewrite history on shared branches.
- Never weaken or skip a test or a verification point to make it pass. If one is wrong or impossible, say so in `implementation.md` and in your reply.
- If the subtask can't be done as specified, stop and explain why rather than improvising a different scope.
