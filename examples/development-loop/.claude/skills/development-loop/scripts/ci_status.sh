#!/bin/sh
# CI status of the current branch's pull request, for the `ci` wait step.
# Exit 0: required checks passed (or the repo has none). Exit 1: a check failed.
# Anything else (e.g. 8 from `gh pr checks` while checks run): not finished yet.
out=$(gh pr checks --required 2>&1)
code=$?
printf '%s\n' "$out"
if [ "$code" -ne 0 ] && printf '%s' "$out" | grep -qiE 'no (required )?checks reported'; then
  exit 0
fi
exit "$code"
