---
name: development-loop
description: "Develop a Jira story end to end: requirements, subtasks, implement and review, then a pull request."
argument-hint: "<run-id> [approve <gate> [note] | reject <gate> <feedback> | retry <step>]"
context: fork
agent: development-loop
background: false
disable-model-invocation: true
---
You were started by the `/development-loop` command, so you are in **command mode**.

Arguments: $ARGUMENTS

Advance the `development-loop` run named in the arguments, as your instructions describe:

1. Apply the user's decision first, if the arguments carry one.
2. Run the loop until the next gate, failure or the end of the flow.
3. Return the report, with the exact `/development-loop …` commands the user can type next.
