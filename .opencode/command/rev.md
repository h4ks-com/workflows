---
description: Pre-commit review gate. Runs the full quality suite and reviews the staged diff before a commit is allowed.
agent: build
---

Run the pre-commit review for this repository.

1. Run `make quality`. If any check fails, report the failure clearly and STOP. Do not create the marker.
2. Inspect the staged change with `git --no-pager diff --staged --stat` and `git --no-pager diff --staged`. Summarize what changed and flag anything risky or untested.
3. Confirm that format, lint, types, imports, dead-code, unused-deps, security, audit, coverage, and build all passed.
4. Only on a full pass, create the marker: `touch .rev-ok`. The commit-guard plugin blocks `git commit` unless this marker exists, and consumes it on commit so the gate must be re-earned each time.
5. Report a concise GO / NO-GO.

$ARGUMENTS
