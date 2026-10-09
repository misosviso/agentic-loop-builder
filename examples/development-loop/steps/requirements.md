# Step: requirements

Part of the `development-loop` flow. You run as a subagent. The orchestrator tells you which files to read, which paths to write, and any feedback from an earlier attempt.

## Goal

Turn the story into precise, testable requirements grounded in the actual codebase. This is the contract the rest of the loop builds and reviews against, and the user approves it at gate **G1**.

## Inputs

- `jira-context.md` from `jira_context`.
- The repository you are running in.

## Outputs

- `requirements.md`, with these sections:
  - **Summary**: two or three sentences on what changes for whom, and why.
  - **Scope**: what is in scope, and explicitly what is out of scope.
  - **Functional requirements**: numbered `R1`, `R2` and so on. Each is one testable statement in the form "When …, the system …". Under each, list its acceptance checks: concrete inputs and expected outputs or behavior.
  - **Non-functional requirements**: performance, security, compatibility and observability, but only those that actually apply.
  - **Codebase findings**: the modules, files, APIs and patterns this will touch or should follow, with paths. Note existing tests near the change.
  - **Assumptions**: each one marked as something the user should confirm at G1.
  - **Open questions**: carried over from `jira-context.md` if still unresolved, plus new ones.

## Procedure

1. Read `jira-context.md` fully.
2. Explore the codebase enough to ground the requirements: find the entry points, the data model and similar features. Cite paths.
3. Write requirements that a reviewer could verify without asking you. Avoid words like "properly", "fast" or "user-friendly" unless you quantify them.
4. Map every acceptance criterion from Jira to at least one `R<n>`, and say which one.
5. If feedback is given, revise the existing `requirements.md` to address every point. Note what changed at the top under **Revision notes**.

## Done when

- [ ] Every Jira acceptance criterion maps to one or more requirements.
- [ ] Every requirement has at least one concrete acceptance check.
- [ ] Out-of-scope items are listed explicitly.
- [ ] Assumptions are visible, not buried in the text.

## Constraints

- Read the code; don't change it.
- Don't plan the implementation here. That is the next step.
