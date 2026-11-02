---
name: improve-codebase-architecture
description: Scan a codebase for deepening opportunities and present them as ranked candidates.
version: 4ae2317
license: MIT
---

# Improve Codebase Architecture

Surface architectural friction and propose **deepening opportunities**: refactors that turn shallow modules into deep ones. The aim is testability and AI-navigability.

This skill is built on a shared design vocabulary. Use these terms exactly in every suggestion, and don't drift into "component," "service," "API," or "boundary":

- **module**, **interface**, **depth**, **seam**, **adapter**, **leverage**, **locality**
- Principles: the deletion test, "the interface is the test surface", "one adapter = hypothetical seam, two = real".

The domain language in `GLOSSARY.md` (if the repository has one) gives names to good seams; ADRs record decisions this skill should not re-litigate. Find them with `get_repo_summary` and `search_repository`.

## Process

### 1. Explore

**Scope before you scan: YAGNI.** Deepening a module pays off by making future changes to it easier, so put extra weight on the parts of the codebase that have recently changed. Decide *where* to look before you look:

- If the task named a direction (a module, a subsystem, a pain point), take it, and skip the inference below.
- Otherwise, use `get_repo_summary` and `get_file_dependants` to find the codebase's hot spots, the files and areas that many others depend on, and let those paths pull your attention first. You have no commit history; if there is no clear hot spot, widen the net.

Read the project's domain glossary (`GLOSSARY.md`) and any ADRs in the area you're touching first.

Then walk the codebase yourself with `list_files`, `read_file`, `find_symbol`, `find_references` and `search_repository`. Don't follow rigid heuristics; explore organically and note where you experience friction:

- Where does understanding one concept require bouncing between many small modules?
- Where are modules **shallow**, with an interface nearly as complex as the implementation?
- Where have pure functions been extracted just for testability, but the real bugs hide in how they're called (no **locality**)?
- Where do tightly-coupled modules leak across their seams?
- Which parts of the codebase are untested, or hard to test through their current interface?

Apply the **deletion test** to anything you suspect is shallow: would deleting it concentrate complexity, or just move it? A "yes, concentrates" is the signal you want.

### 2. Present candidates

Report the candidates in the `summary` and `notes` of `finish`, as plain text. Do not write report files, and do not edit code unless the task asks for it.

For each candidate give:

- **Files**: which files/modules are involved
- **Problem**: why the current architecture is causing friction
- **Solution**: plain English description of what would change
- **Benefits**: explained in terms of locality and leverage, and how tests would improve
- **Before / After**: a short text sketch of the shallowness and the deepening
- **Recommendation strength**: one of `Strong`, `Worth exploring`, `Speculative`

End with a **Top recommendation**: which candidate you'd tackle first and why.

**Use GLOSSARY.md vocabulary for the domain, and the design vocabulary above for the architecture.** If `GLOSSARY.md` defines "Order," talk about "the Order intake module," not "the FooBarHandler," and not "the Order service."

**ADR conflicts**: if a candidate contradicts an existing ADR, only surface it when the friction is real enough to warrant revisiting the ADR. Mark it clearly (e.g. _"contradicts ADR-0007, but worth reopening because…"_). Don't list every theoretical refactor an ADR forbids.

Do NOT propose interfaces yet.
