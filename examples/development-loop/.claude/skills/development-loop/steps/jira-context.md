# Step: jira_context

Part of the `development-loop` flow. You run as a subagent. The orchestrator tells you which files to read, which paths to write, and any feedback from an earlier attempt.

## Goal

Collect everything known about the story into one self-contained file. Every later step reads this file instead of going back to Jira, so leave nothing relevant out and invent nothing.

## Inputs

- Run input `story`: the Jira issue key, e.g. `PROJ-123`.

## Outputs

- `jira-context.md`, with these sections in this order:
  - `# <KEY>: <summary>`
  - **Metadata**: type, status, priority, assignee, reporter, labels, components, fix version, sprint, and a link to the issue.
  - **Description**: the issue description verbatim, converted to Markdown.
  - **Acceptance criteria**: verbatim if the issue has them, otherwise "none stated".
  - **Linked issues**: the parent or epic, subtasks, blocks and is-blocked-by, and relates-to. Give each a key, summary, status and one line on why it matters.
  - **Discussion**: comments that change or clarify scope, quoted with author and date. Skip noise.
  - **Attachments and links**: names and URLs, plus a one-line note on what each one is.
  - **Open questions**: contradictions, gaps and unclear terms you noticed. Don't resolve them here.

## Procedure

1. Fetch the issue using the first source that works:
   - a Jira or Atlassian MCP tool, if one is available;
   - the `acli` or `jira` CLI;
   - the Jira REST API, `GET /rest/api/3/issue/<KEY>?expand=renderedFields`, using credentials from the environment (`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`).
2. Fetch the parent or epic and directly linked issues, one level deep only.
3. Write `jira-context.md`.
4. If no source works, don't guess. Write a `jira-context.md` that contains only the heading and a note that Jira was unreachable, and explain this in your reply. The orchestrator will ask the user to paste the story.

## Done when

- [ ] Every section above is present, using "none" where it is empty.
- [ ] The description and acceptance criteria are verbatim, not paraphrased.
- [ ] Nothing in the file is inferred. Anything uncertain sits under **Open questions**.

## Constraints

- Read-only: never transition, edit, comment on, or assign the issue.
- Never write credentials or tokens into the artifact.
