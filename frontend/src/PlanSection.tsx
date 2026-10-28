import { useCallback, useEffect, useState } from "react";
import ProjectHeader from "./ProjectHeader";
import { StatusBadge } from "./ui";
import {
  buildPlan,
  decidePlan,
  getPlan,
  listPlans,
  listProposals,
  listRequirements,
  type ImplementationPlan,
  type PlanSummary,
  type PlanTask,
  type ProposalSummary,
} from "./api";

const AI_FLAG_PREFIX = "AI recommendation";

function TaskCard({
  task,
  titles,
}: {
  task: PlanTask;
  titles: Map<string, string>;
}) {
  return (
    <li className="card" data-testid={`task-${task.key}`}>
      <div className="title-row">
        <h3>
          {task.key} · {task.title}
        </h3>
        <span className="badge badge-type">{task.kind}</span>
      </div>
      {task.description && <p className="sub">{task.description}</p>}
      {task.flags.length > 0 && (
        <ul aria-label={`Flags for ${task.key}`}>
          {task.flags.map((f) => (
            <li key={f}>
              <span
                className={`badge ${f.startsWith(AI_FLAG_PREFIX) ? "badge-ai" : "badge-warn"}`}
              >
                {f}
              </span>
            </li>
          ))}
        </ul>
      )}
      <p>
        <strong>Depends on:</strong>{" "}
        {task.depends_on.length > 0 ? task.depends_on.join(", ") : "none"}
      </p>
      <div>
        <strong>Linked requirements</strong>
        {task.requirement_ids.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <ul aria-label={`Requirements for ${task.key}`}>
            {task.requirement_ids.map((id) => (
              <li key={id}>{titles.get(id) ?? id}</li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <strong>Acceptance criteria</strong>
        {task.acceptance_criteria.length === 0 ? (
          <p className="muted">None recorded.</p>
        ) : (
          <ul aria-label={`Acceptance criteria for ${task.key}`}>
            {task.acceptance_criteria.map((c, i) => (
              <li key={i}>
                {c}
                {c.includes("needs clarification") && (
                  <>
                    {" "}
                    <span className="badge badge-warn">Needs clarification</span>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
      {task.blocking_questions.length > 0 && (
        <div role="note">
          <strong>Blocking questions</strong>
          <ul aria-label={`Blocking questions for ${task.key}`}>
            {task.blocking_questions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ul>
        </div>
      )}
    </li>
  );
}

export default function PlanSection({
  projectId,
  projectName,
  projectDescription,
}: {
  projectId: string;
  projectName?: string | null;
  projectDescription?: string | null;
}) {
  const [summaries, setSummaries] = useState<PlanSummary[] | null>(null);
  const [selected, setSelected] = useState<ImplementationPlan | null>(null);
  const [proposals, setProposals] = useState<ProposalSummary[]>([]);
  const [titles, setTitles] = useState<Map<string, string>>(new Map());
  const [confirmedCount, setConfirmedCount] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const [list, props, reqs] = await Promise.all([
        listPlans(projectId),
        listProposals(projectId),
        listRequirements(projectId),
      ]);
      setTitles(new Map(reqs.map((r) => [r.id, r.title])));
      setConfirmedCount(reqs.filter((r) => r.status === "confirmed").length);
      setProposals(props);
      setSummaries(list);
      setSelected(list.length > 0 ? await getPlan(projectId, list[0].id) : null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Failed to load plan");
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(fn: () => Promise<ImplementationPlan>, fallback: string) {
    setBusy(true);
    setError(null);
    try {
      const plan = await fn();
      setSelected(plan);
      setSummaries((cur) => {
        const { tasks, unattached_questions, ...summary } = plan;
        void tasks;
        void unattached_questions;
        const rest = (cur ?? []).filter((s) => s.id !== plan.id);
        return [{ ...summary, task_count: tasks.length }, ...rest].sort(
          (a, b) => b.version - a.version,
        );
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : fallback);
    } finally {
      setBusy(false);
    }
  }

  const draft = selected?.status === "draft";
  const approvedKey = (summaries ?? [])
    .filter((s) => s.status === "approved")
    .map((s) => s.id)
    .join(",");

  return (
    <section aria-label="Plan" className="stack">
      <ProjectHeader
        key={approvedKey}
        projectId={projectId}
        name={projectName}
        description={projectDescription}
        proposals={proposals}
        confirmedCount={confirmedCount}
      />
      <div className="card">
        <h2>Implementation plan</h2>
        <p className="sub">
          Built deterministically from the latest approved architecture. Each build creates a new
          draft version; approved plans are never edited. No code is generated.
        </p>
        <button
          type="button"
          className="primary"
          disabled={busy}
          onClick={() => void act(() => buildPlan(projectId), "Plan build failed")}
        >
          {busy ? "Working…" : summaries && summaries.length > 0 ? "Rebuild plan" : "Build plan"}
        </button>
        {error && <p role="alert">{error}</p>}
      </div>

      {loadError && (
        <div role="alert">
          <p>{loadError}</p>
          <button type="button" onClick={() => void load()}>
            Retry plan
          </button>
        </div>
      )}
      {!loadError && summaries === null && <p className="muted">Loading plan…</p>}
      {summaries && summaries.length === 0 && <p className="state">No plan built yet.</p>}

      {selected && (
        <div data-testid="plan">
          <div className="card">
            <ul className="arch-tabs" aria-label="Plan versions">
              {(summaries ?? []).map((s) => (
                <li key={s.id}>
                  <button
                    type="button"
                    className="tab"
                    aria-pressed={selected.id === s.id}
                    onClick={() =>
                      void getPlan(projectId, s.id)
                        .then(setSelected)
                        .catch((e) =>
                          setError(e instanceof Error ? e.message : "Failed to load plan"),
                        )
                    }
                  >
                    V{s.version}
                    <span className="visually-hidden"> ({s.status})</span>
                  </button>
                </li>
              ))}
            </ul>
            <div className="title-row">
              <h2>Plan version {selected.version}</h2>
              <StatusBadge status={selected.status} testId="plan-status" />
            </div>
            <p className="sub">{selected.tasks.length} task(s) in implementation order.</p>
            <div className="btn-row">
              <button
                type="button"
                className="approve"
                disabled={busy || !draft}
                onClick={() => void act(() => decidePlan(projectId, selected.id, "approve"), "Approve failed")}
              >
                Approve
              </button>
              <button
                type="button"
                className="danger"
                disabled={busy || !draft}
                onClick={() => void act(() => decidePlan(projectId, selected.id, "reject"), "Reject failed")}
              >
                Reject
              </button>
            </div>
            {!draft && <p className="hint">Reviewed plans are read-only. Rebuild to create a new version.</p>}
          </div>

          {selected.unattached_questions.length > 0 && (
            <div className="card">
              <h3>Open questions not tied to a task</h3>
              <ul aria-label="Unattached questions">
                {selected.unattached_questions.map((q, i) => (
                  <li key={i}>{q}</li>
                ))}
              </ul>
            </div>
          )}

          <ol className="stack" aria-label="Plan tasks">
            {selected.tasks.map((t) => (
              <TaskCard key={t.id} task={t} titles={titles} />
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}
