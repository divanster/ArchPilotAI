# Minimising a failure

Cut one thing at a time, re-run after every cut, keep only what makes the failure go away when
removed.

- Tests: run one test, then strip its setup lines, then its assertions down to the failing one.
- Inputs: halve the data, then halve again; keep the smallest input that still fails.
- Code: replace a collaborator with a constant or a stub and see whether the failure stays.
- Config and order: run the test alone and in the suite; if it only fails in the suite, an
  earlier test leaks state (shared fixture, global, time, random seed).
- Type or lint failures: reduce to the one file and the one line the checker names.

Done when removing any remaining element makes the failure disappear.
