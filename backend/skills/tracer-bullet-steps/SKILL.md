---
name: tracer-bullet-steps
description: Break work into tracer-bullet steps, thin vertical slices that each work end to end. Every step lists its files, the acceptance criteria it covers and exactly one allowed verification command. Use when planning or ordering the steps of a task or checklist.
version: 1.0.0
---

# Tracer-bullet steps

A tracer bullet is a thin slice through every layer that works end to end, however small. Prefer
it over finishing one layer at a time. After step 1 the feature runs, crudely; every later step
makes it more complete.

## Rules

1. **Vertical, not horizontal.** A step touches whichever layers it needs (data, logic, API,
   UI) for one narrow behaviour. "Write all the models" or "add all the tests last" is a
   horizontal step; split it.
2. **Each step works alone.** When a step is done the code is valid: static checks pass and the
   behaviour of that step can be shown. No step leaves the draft broken for the next one.
3. **Smallest first.** Step 1 is the thinnest path that exercises the whole shape. Later steps
   add one behaviour, one rule or one edge case each.
4. **Cover every criterion.** Each acceptance criterion of the task is covered by at least one
   step; list the ones a step covers by their text or number. A criterion that no step covers is
   a gap: add a step or say why it is out of scope.
5. **Dependencies are explicit.** A step names the earlier steps it needs. No cycles.

## Step format

Write each step like this:

```
Step N: <what works after this step, one sentence>
Files: <path>, <path> (new files marked "new")
Covers: <acceptance criteria, quoted or numbered>
Needs: <earlier steps, or "none">
Verify: <exactly one command id>
```

The verification command is one id of the allowlist: `pytest`, `ruff`, `mypy`, `tsc` or
`npm test`. Pick the one that proves this step (for a backend behaviour, `pytest`; for a
type-level change, `mypy` or `tsc`). Never write a shell command line, a path or flags: only the
id is allowed, and it runs through the execution provider when `run_tests` is offered.

## Process

1. Read the task (`get_task`) and its criteria (`get_requirements`).
2. Look at the code that exists (`get_repo_summary`, `search_repository`, `find_symbol`,
   `get_file_dependants`) so the files you list are real, and new files are marked as new.
3. Draft the steps, then check rule 4: walk every criterion and find its step.
4. Check each step against rules 1 and 2. If a step cannot be verified with one command, it is
   too big or too vague: split or sharpen it.
5. Report the steps in this format. Do not start implementing steps beyond what the task asks.
