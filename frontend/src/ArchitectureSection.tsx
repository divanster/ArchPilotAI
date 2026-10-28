import { createContext, useCallback, useContext, useEffect, useState } from "react";
import ArchitectureDiagram from "./ArchitectureDiagram";
import { StatusBadge } from "./ui";
import { hrefFor } from "./routes";
import {
  generateArchitecture,
  getProposal,
  listProposals,
  listRequirements,
  updateProposal,
  type ArchitectureProposal,
  type Decision,
  type CoverageEntry,
  type CoverageStatus,
  type DecisionOrigin,
  type LintReport,
  type LintRule,
  type PassageSource,
  type RetrievalInfo,
  type ProposalPatch,
  type ProposalSummary,
} from "./api";

const ORIGIN_LABELS: Record<DecisionOrigin, string> = {
  required_by_client: "Required by client",
  preferred_by_client: "Client preference",
  recommended_by_ai: "Recommended by AI",
  assumed: "Assumed",
  requires_clarification: "Requires clarification",
};

const COVERAGE_LABELS: Record<CoverageStatus, string> = {
  addressed: "Addressed",
  partial: "Partial",
  unresolved: "Unresolved",
};

type Titles = Map<string, string>;

const LINT_GROUPS: { label: string; rules: LintRule[] }[] = [
  { label: "Origin", rules: ["origin_overclaim"] },
  { label: "Wording", rules: ["completed_implementation_wording"] },
  {
    label: "Coverage",
    rules: ["coverage_all_addressed", "frontend_only_backend_requirement"],
  },
  { label: "Citations", rules: ["passages_not_cited"] },
];

function ReviewFindings({ report }: { report: LintReport | null | undefined }) {
  if (!report) {
    return (
      <section className="card" aria-labelledby="review-findings" data-testid="review-findings">
        <h3 id="review-findings">Review findings</h3>
        <p className="muted" data-testid="lint-not-run">
          Lint not run for this version
        </p>
      </section>
    );
  }
  const groups = LINT_GROUPS.map((g) => ({
    label: g.label,
    items: report.findings.filter((f) => g.rules.includes(f.rule)),
  })).filter((g) => g.items.length > 0);
  return (
    <section className="card" aria-labelledby="review-findings" data-testid="review-findings">
      <h3 id="review-findings">Review findings</h3>
      <p className="muted">
        Advisory only: automatic checks for a person to weigh. They never block
        approval.
      </p>
      {groups.length === 0 ? (
        <p data-testid="lint-empty">No findings.</p>
      ) : (
        groups.map((g) => (
          <div key={g.label} data-testid="lint-group">
            <h4>
              {g.label} ({g.items.length})
            </h4>
            <ul>
              {g.items.map((f, i) => (
                <li key={`${f.rule}-${f.path ?? ""}-${i}`} data-testid="lint-finding">
                  {f.message}
                  {f.path && <> — <code>{f.path}</code></>}
                  {f.terms.length > 0 && <> ({f.terms.join(", ")})</>}
                </li>
              ))}
            </ul>
          </div>
        ))
      )}
    </section>
  );
}

interface SourceContext {
  projectId: string;
  sources: Map<string, PassageSource>;
}

const Sources = createContext<SourceContext>({ projectId: "", sources: new Map() });

const RETRIEVAL_TEXT: Record<RetrievalInfo["status"], string> = {
  used: "Keyword search supplied unconfirmed document passages as context.",
  no_matches: "Documents are indexed, but no passages matched the requirements.",
  not_indexed: "Document context not used: documents are not indexed.",
  no_documents: "Document context not used: the project has no documents.",
  disabled: "Document context was disabled for this generation.",
};

function RetrievalNote({ info }: { info: RetrievalInfo | null | undefined }) {
  if (!info) {
    return (
      <p className="muted" data-testid="retrieval-note">
        Document context not recorded
      </p>
    );
  }
  const extra: string[] = [];
  if (info.status === "used") extra.push(`${info.passages_provided} passage(s)`);
  if (info.unindexed_documents > 0 && info.status !== "not_indexed")
    extra.push(`${info.unindexed_documents} document(s) not indexed`);
  if (info.truncated) extra.push("limited by the context budget");
  return (
    <p className="muted" data-testid="retrieval-note">
      {RETRIEVAL_TEXT[info.status]}
      {extra.length > 0 && ` (${extra.join("; ")})`} Passages are evidence, not
      requirements.
    </p>
  );
}

function Citations({ ids }: { ids: string[] | undefined }) {
  const { projectId, sources } = useContext(Sources);
  if (!ids || ids.length === 0) return null;
  return (
    <ul className="chip-list" aria-label="Cited passages">
      {ids.map((id) => {
        const s = sources.get(id);
        if (!s) return <li className="chip" key={id}>Source unavailable</li>;
        return (
          <li className="chip" key={id} data-testid="cited-passage">
            <a href={hrefFor("documents", projectId, s.document_id)}>
              {s.filename} [{s.source_start}–{s.source_end}]
            </a>
          </li>
        );
      })}
    </ul>
  );
}

function CoverageMatrix({
  matrix,
  titles,
  elements,
}: {
  matrix: CoverageEntry[] | null | undefined;
  titles: Titles;
  elements: Map<string, string>;
}) {
  if (!matrix) {
    return (
      <div className="card">
        <h3>Requirement coverage</h3>
        <p className="muted" data-testid="coverage-not-recorded">
          Coverage not recorded
        </p>
      </div>
    );
  }
  const counts = { addressed: 0, partial: 0, unresolved: 0 };
  for (const e of matrix) counts[e.status] += 1;
  return (
    <div className="card">
      <h3>Requirement coverage</h3>
      <p className="muted" data-testid="coverage-summary">
        {counts.addressed} addressed, {counts.partial} partial, {counts.unresolved} unresolved.
        This matrix is the model's own claim; a person must review it.
      </p>
      <div className="diagram-wrap">
        <table aria-label="Requirement coverage">
          <thead>
            <tr>
              <th>Requirement</th>
              <th>Components / decisions</th>
              <th>Proposed verification</th>
              <th>Status</th>
              <th>Gap or clarification</th>
            </tr>
          </thead>
          <tbody>
            {matrix.map((e) => (
              <tr key={e.requirement_id} data-testid="coverage-row">
                <td>{titles.get(e.requirement_id) ?? "Unknown requirement"}</td>
                <td>
                  {e.related_ids.length === 0
                    ? "None"
                    : e.related_ids.map((id) => elements.get(id) ?? id).join(", ")}
                </td>
                <td>{e.verification}</td>
                <td>
                  <span className={`badge coverage-${e.status}`}>
                    {COVERAGE_LABELS[e.status]}
                  </span>
                </td>
                <td>{e.gap ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Refs({ ids, titles }: { ids: string[]; titles: Titles }) {
  if (ids.length === 0) return null;
  return (
    <ul className="chip-list" aria-label="Supporting requirements">
      {ids.map((id) => (
        <li className="chip" key={id}>
          {titles.get(id) ?? "Unknown requirement"}
        </li>
      ))}
    </ul>
  );
}

function TextList({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <>
      <dt>{label}</dt>
      <dd>{items.join("; ")}</dd>
    </>
  );
}

function DecisionCard({
  decision,
  titles,
  heading,
}: {
  decision: Decision;
  titles: Titles;
  heading?: string;
}) {
  return (
    <li className="decision">
      <h4>{heading ?? decision.name}</h4>
      <span className={`badge origin-${decision.origin}`}>
        {ORIGIN_LABELS[decision.origin]}
      </span>
      <p>{decision.decision}</p>
      <dl>
        <dt>Rationale</dt>
        <dd>{decision.rationale}</dd>
        <TextList label="Alternatives considered" items={decision.alternatives_considered} />
        <TextList label="Assumptions" items={decision.assumptions} />
        <TextList label="Risks" items={decision.risks} />
      </dl>
      <Refs ids={decision.supporting_requirement_ids} titles={titles} />
      <Citations ids={decision.source_passage_ids} />
    </li>
  );
}

function DecisionGroup({
  title,
  items,
  titles,
}: {
  title: string;
  items: Decision[];
  titles: Titles;
}) {
  if (items.length === 0) return null;
  return (
    <div className="card">
      <h3>{title}</h3>
      <ul className="stack" aria-label={title}>
        {items.map((d, i) => (
          <DecisionCard key={i} decision={d} titles={titles} />
        ))}
      </ul>
    </div>
  );
}

function ProposalView({
  proposal,
  titles,
  projectId,
  onPatch,
}: {
  proposal: ArchitectureProposal;
  projectId: string;
  titles: Titles;
  onPatch: (patch: ProposalPatch) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(proposal.title);
  const [summary, setSummary] = useState(proposal.summary);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const c = proposal.content;
  const draft = proposal.status === "draft";
  const stale = proposal.stale_requirement_ids;
  const sources = new Map((c.source_passages ?? []).map((s) => [s.chunk_id, s]));
  const elements = new Map<string, string>();
  for (const comp of c.components) elements.set(comp.id, comp.name);
  for (const d of [
    ...c.recommended_stack,
    ...c.integrations,
    ...c.data_architecture,
    ...c.security_architecture,
    ...c.non_functional_design,
  ]) {
    if (d.id) elements.set(d.id, d.name);
  }

  async function run(patch: ProposalPatch, after?: () => void) {
    setBusy(true);
    setError(null);
    try {
      await onPatch(patch);
      after?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Update failed");
    } finally {
      setBusy(false);
    }
  }

  function saveEdit() {
    if (!title.trim() || !summary.trim()) {
      setError("Title and summary are required");
      return;
    }
    const patch: ProposalPatch = {};
    if (title.trim() !== proposal.title) patch.title = title.trim();
    if (summary.trim() !== proposal.summary) patch.summary = summary.trim();
    if (Object.keys(patch).length === 0) {
      setEditing(false);
      return;
    }
    void run(patch, () => setEditing(false));
  }

  return (
    <Sources.Provider value={{ projectId, sources }}>
    <div className="stack" data-testid="proposal">
      <div className="card">
        <div className="row">
          <StatusBadge status={proposal.status} testId="proposal-status" />
          <span className="muted">
            Version {proposal.version} · {proposal.provider}/{proposal.model} ·{" "}
            {proposal.prompt_version}
          </span>
        </div>
        {editing ? (
          <div className="stack" style={{ marginTop: "var(--space-3)" }}>
            <label>
              Proposal title
              <input
                value={title}
                maxLength={200}
                onChange={(e) => setTitle(e.target.value)}
              />
            </label>
            <label>
              Proposal summary
              <textarea
                value={summary}
                maxLength={5000}
                onChange={(e) => setSummary(e.target.value)}
              />
            </label>
            <div className="row">
              <button type="button" className="primary" disabled={busy} onClick={saveEdit}>
                Save proposal
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setEditing(false);
                  setTitle(proposal.title);
                  setSummary(proposal.summary);
                  setError(null);
                }}
              >
                Cancel edit
              </button>
            </div>
          </div>
        ) : (
          <>
            <h2 style={{ marginTop: "var(--space-3)" }}>{proposal.title}</h2>
            <p>{proposal.summary}</p>
          </>
        )}
        {stale.length > 0 && (
          <p role="alert">
            Warning: {stale.length} cited requirement(s) are no longer
            confirmed. Regenerate this proposal before approving.
          </p>
        )}
        {error && <p role="alert">{error}</p>}
        {!editing && (
          <ReviewFindings report={proposal.lint_findings} />
        )}
        {!editing && (
          <div className="row">
            <button
              type="button"
              className="primary"
              disabled={busy || !draft || stale.length > 0}
              onClick={() => void run({ status: "approved" })}
            >
              Approve
            </button>
            <button
              type="button"
              className="danger"
              disabled={busy || !draft}
              onClick={() => void run({ status: "rejected" })}
            >
              Reject
            </button>
            <button type="button" disabled={busy || !draft} onClick={() => setEditing(true)}>
              Edit
            </button>
          </div>
        )}
        {!draft && (
          <p className="hint">Reviewed proposals are read-only. Generate a new version to change them.</p>
        )}
      </div>

      <div className="card">
        <h3>Solution overview</h3>
        <p>{c.solution_overview}</p>
      </div>

      <RetrievalNote info={c.retrieval} />
      <CoverageMatrix matrix={c.coverage_matrix} titles={titles} elements={elements} />

      <div className="card">
        <h3>Component diagram</h3>
        <ArchitectureDiagram nodes={c.diagram.nodes} edges={c.diagram.edges} />
      </div>

      <div className="card">
        <h3>Technology stack</h3>
        <ul className="stack" aria-label="Technology stack">
          {c.recommended_stack.map((s, i) => (
            <DecisionCard
              key={i}
              decision={s}
              titles={titles}
              heading={`${s.layer}: ${s.technology}`}
            />
          ))}
        </ul>
      </div>

      <div className="card">
        <h3>Components</h3>
        <ul className="stack" aria-label="Components">
          {c.components.map((comp) => (
            <li className="decision" key={comp.id}>
              <h4>{comp.name}</h4>
              <span className="badge badge-type">{comp.type}</span>
              <p>{comp.responsibility}</p>
              <Refs ids={comp.requirement_ids} titles={titles} />
              <Citations ids={comp.source_passage_ids} />
            </li>
          ))}
        </ul>
      </div>

      <DecisionGroup title="Integrations" items={c.integrations} titles={titles} />
      <DecisionGroup title="Data architecture" items={c.data_architecture} titles={titles} />
      <DecisionGroup title="Security considerations" items={c.security_architecture} titles={titles} />
      <DecisionGroup title="Non-functional design" items={c.non_functional_design} titles={titles} />

      <div className="card">
        <h3>Risks and assumptions</h3>
        <ul className="stack" aria-label="Risks">
          {c.risks.map((r, i) => (
            <li className="decision" key={i}>
              <span className="badge badge-type">{r.severity}</span>
              <p>{r.description}</p>
              <p className="muted">Mitigation: {r.mitigation}</p>
              <Refs ids={r.requirement_ids} titles={titles} />
              <Citations ids={r.source_passage_ids} />
            </li>
          ))}
        </ul>
        <ul aria-label="Assumptions">
          {c.assumptions.map((a, i) => (
            <li key={i}>
              Assumption: {a.statement}
              <Citations ids={a.source_passage_ids} />
            </li>
          ))}
        </ul>
      </div>

      <div className="card">
        <h3>Open questions</h3>
        {c.open_questions.length === 0 && <p className="muted">None.</p>}
        <ul aria-label="Open questions">
          {c.open_questions.map((q, i) => (
            <li key={i}>
              {q.question}
              {q.why_it_matters && <span className="muted"> — {q.why_it_matters}</span>}
              <Citations ids={q.source_passage_ids} />
            </li>
          ))}
        </ul>
      </div>

      {c.alternatives.length > 0 && (
        <div className="card">
          <h3>Alternatives</h3>
          <ul aria-label="Alternatives">
            {c.alternatives.map((a, i) => (
              <li key={i}>
                <strong>{a.title}</strong>: {a.description} ({a.trade_offs})
                <Citations ids={a.source_passage_ids} />
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="card">
        <h3>Implementation roadmap</h3>
        <ol aria-label="Implementation roadmap">
          {c.implementation_phases.map((p, i) => (
            <li key={i}>
              <strong>{p.name}</strong>
              <ul>
                {p.objectives.map((o, j) => (
                  <li key={j}>{o}</li>
                ))}
              </ul>
            </li>
          ))}
        </ol>
      </div>
    </div>
    </Sources.Provider>
  );
}

export default function ArchitectureSection({ projectId }: { projectId: string }) {
  const [summaries, setSummaries] = useState<ProposalSummary[] | null>(null);
  const [selected, setSelected] = useState<ArchitectureProposal | null>(null);
  const [titles, setTitles] = useState<Titles>(new Map());
  const [loadError, setLoadError] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [genError, setGenError] = useState<string | null>(null);

  const open = useCallback(
    async (id: string) => {
      setSelected(await getProposal(projectId, id));
    },
    [projectId],
  );

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const [list, reqs] = await Promise.all([
        listProposals(projectId),
        listRequirements(projectId),
      ]);
      setTitles(new Map(reqs.map((r) => [r.id, r.title])));
      setSummaries(list);
      setSelected(list.length > 0 ? await getProposal(projectId, list[0].id) : null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Failed to load architecture");
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleGenerate() {
    setGenerating(true);
    setGenError(null);
    try {
      const created = await generateArchitecture(projectId);
      const reqs = await listRequirements(projectId);
      setTitles(new Map(reqs.map((r) => [r.id, r.title])));
      setSummaries((cur) => [created, ...(cur ?? [])]);
      setSelected(created);
    } catch (e) {
      setGenError(e instanceof Error ? e.message : "Generation failed");
    } finally {
      setGenerating(false);
    }
  }

  async function handlePatch(patch: ProposalPatch) {
    if (!selected) return;
    const updated = await updateProposal(projectId, selected.id, patch);
    setSelected(updated);
    setSummaries((cur) => cur?.map((s) => (s.id === updated.id ? updated : s)) ?? null);
  }

  return (
    <section aria-label="Architecture" className="stack">
      <div className="card">
        <h2>Generate architecture</h2>
        <p className="muted">
          Proposals are generated from confirmed requirements only. Every
          proposal starts as a draft and must be reviewed by a person.
        </p>
        <button
          type="button"
          className="primary"
          disabled={generating}
          onClick={() => void handleGenerate()}
        >
          {generating ? "Generating…" : "Generate architecture"}
        </button>
        {genError && <p role="alert">{genError}</p>}
      </div>

      {loadError && (
        <div role="alert">
          <p>{loadError}</p>
          <button type="button" onClick={() => void load()}>
            Retry architecture
          </button>
        </div>
      )}
      {!loadError && summaries === null && (
        <p className="muted">Loading architecture…</p>
      )}
      {summaries && summaries.length === 0 && (
        <p className="state">No architecture proposals yet.</p>
      )}
      {summaries && summaries.length > 0 && (
        <ul className="arch-tabs" aria-label="Proposal versions">
          {summaries.map((s) => (
            <li key={s.id}>
              <button
                type="button"
                aria-pressed={selected?.id === s.id}
                onClick={() =>
                  void open(s.id).catch((e) =>
                    setLoadError(e instanceof Error ? e.message : "Failed to load proposal"),
                  )
                }
              >
                Version {s.version} ({s.status})
              </button>
            </li>
          ))}
        </ul>
      )}
      {selected && (
        <ProposalView
          key={selected.id}
          proposal={selected}
          titles={titles}
          projectId={projectId}
          onPatch={handlePatch}
        />
      )}
    </section>
  );
}
