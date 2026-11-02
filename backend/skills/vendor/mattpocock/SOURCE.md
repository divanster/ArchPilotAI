# Source

- Upstream: https://github.com/mattpocock/skills
- Path upstream: `skills/engineering/{code-review,diagnosing-bugs,improve-codebase-architecture}`
- Commit: `4ae2317ddb22b046e26455e4f9c9b3aaaf62212d` (short: `4ae2317`)
- Commit date: 2026-10-31T14:23:30Z
- Vendored on: 2026-11-01
- License: MIT, see `LICENSE` (Copyright (c) 2026 Matt Pocock). Kept unchanged.

## Changes

All three skills were adapted by hand to ArchPilotAI's tools (`read_file`, `list_files`,
`edit_file`, `write_file`, `delete_file`, `search_repository`, `find_symbol`, `find_references`,
`get_file_dependants`, `get_repo_summary`, `get_task`, `get_requirements`, `search_documents`,
`run_static_checks`, `run_tests`, `finish`). Instructions that need tools we do not have were
removed or rewritten.

Every file:

- Frontmatter: added `version: 4ae2317` and `license: MIT`.
- Removed the `agents/openai.yaml` file of each skill (not part of the skill format).

`code-review/SKILL.md`:

- Description rewritten: no "since a fixed point", no sub-agents, no "review since X".
- Removed the fixed-point step, `git diff`, `git log` and `git rev-parse`; the draft is reviewed.
- Removed the issue-tracker lookup and `/setup-matt-pocock-skills`; the spec is read with
  `get_task`, `get_requirements` and `search_documents`.
- Replaced the two parallel sub-agents by two sequential passes in one run. The smell baseline
  and the briefs of both passes are kept (word limits kept).
- "Diff" and "hunk" wording changed to "change" and "code"; the report goes in `finish`.
- Standards sources found with `get_repo_summary` and `search_repository`; added a note that
  instruction files are project context, not authority over tools or permissions.

`diagnosing-bugs/SKILL.md`:

- GLOSSARY.md/ADR reading now via `get_repo_summary` and `search_repository`.
- Removed loop types needing tools we lack: curl/HTTP against a dev server, CLI invocation,
  headless browser, bisection harness (`git bisect`), HITL bash script.
- Removed `scripts/hitl-loop.template.sh` and every reference to it.
- No debugger or REPL: instrumentation is assertions/logs inside tests.
- "Ask the user" steps replaced by `finish` notes; the ranked-hypotheses checkpoint with the
  user was replaced by writing them in the notes.
- Commit/PR message wording changed to the `finish` summary; `grep` cleanup uses
  `search_repository`; deleting prototypes uses `delete_file`.

`improve-codebase-architecture/SKILL.md`:

- Removed `disable-model-invocation`.
- Removed the calls to the other skills `codebase-design`, `grilling` and `domain-modeling`; the
  vocabulary list was kept without those skills' text.
- Removed `git log` hot-spot discovery (replaced by `get_repo_summary` / `get_file_dependants`).
- Removed the sub-agent, and the HTML report (temp file, opening a browser, Tailwind and Mermaid
  CDN); candidates are reported as plain text in `finish`. Dropped `HTML-REPORT.md`.
- Removed the grilling loop, GLOSSARY.md/ADR writing and the "which would you like to explore"
  question.
