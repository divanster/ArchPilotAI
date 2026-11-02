---
name: diagnose-ci-failure
description: Work a failing CI or test run to a fix in a fixed order, reproduce, minimise, hypothesise, fix, regression test. Use when run_tests or CI reports failures that you must repair.
version: 1.0.0
---

# Diagnose a CI failure

Do the phases in order. Do not edit code before step 1 is done, and do not skip step 5.

## 1. Reproduce

Reproduce the failure with the same command that failed: `run_tests` with the failing command
id (`pytest`, `ruff`, `mypy`, `tsc` or `npm test`). Read the new failures in changed files first;
failures elsewhere and pre-existing ones are counted, not yours to fix unless the task says so.
If you cannot reproduce it, say so and stop: a guess is not a fix. Note the exact symptom (error
type, message, test name, file and line); every later step must be checked against it.

Flaky results (same code, different outcome) are a finding by themselves: report them and do not
"fix" them by retrying.

## 2. Minimise

Shrink the failure to the smallest case that still fails: one test instead of the suite, one
input instead of many, one file instead of several. Cut one thing at a time and re-check. Every
element that remains must be load-bearing. See `references/minimise.md` (read it with
`read_skill_file`) for a short checklist.

## 3. Hypothesise

Write 3 to 5 ranked hypotheses before testing any. Each one is falsifiable: "If X is the cause,
then changing Y makes the failure go away". A hypothesis without a prediction is a guess; sharpen
or drop it. Use `read_file`, `search_repository`, `find_symbol`, `find_references` and
`get_file_dependants` to check them against the code. Change one thing at a time.

## 4. Fix

Fix the cause the surviving hypothesis names, in the smallest edit (`edit_file`). Do not
disable, skip or delete a failing test, loosen an assertion, or add a broad `except` to make it
pass; those hide the failure. Do not touch unrelated code. If the cause is outside the files you
may change, report it in the `finish` notes.

## 5. Regression test

The failure must stay fixed. If a test already caught it, run it again and it is your regression
test. If not, add a test that fails without the fix and passes with it (`write_file` for a new
test file, `edit_file` for an existing one), at the seam where the bug really happens. If there
is no such seam, say so in the notes.

Then run `run_static_checks` and `run_tests` once more with the command that failed, and finish
only when the original symptom is gone.
