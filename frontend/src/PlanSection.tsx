import { useCallback, useEffect, useRef, useState } from "react";
import ProjectHeader from "./ProjectHeader";
import { StatusBadge } from "./ui";
import {
  buildPlan,
  decidePlan,
  editPlanDependency,
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

interface EditorFields {
  reason: string;
  author: string;
  setReason: (v: string) => void;
  setAuthor: (v: string) => void;
}

// Adding task -> target closes a cycle when target already depends on task, directly or not.
function wouldCycle(allTasks: PlanTask[], task: PlanTask, target: PlanTask): boolean {
  const byKey = new Map(allTasks.map((t) => [t.key, t]));
  const seen = new Set<string>();
  const stack = [target.key];
  while (stack.length > 0) {
    const key = stack.pop() as string;
    if (key === task.key) return true;
    if (seen.has(key)) continue;
    seen.add(key);
    for (const next of byKey.get(key)?.depends_on ?? []) stack.push(next);
  }
  return false;
}

function stripTrailingHeading(text: string): string {
  const trimmed = text.trimEnd();
  const idx = trimmed.lastIndexOf("\n");
  const last = trimmed.slice(idx + 1);
  if (!/^\s*\d+\.\s+[A-Z][\w ]+$/.test(last)) return text;
  return trimmed.slice(0, idx < 0 ? 0 : idx).trimEnd();
}

function DependencyEditor({
  task,
  allTasks,
  fields,
  onClose,
  onEdit,
}: {
  task: PlanTask;
  allTasks: PlanTask[];
  fields: EditorFields;
  onClose: () => void;
  onEdit: (
    action: "add" | "remove",
    task: PlanTask,
    target: PlanTask,
    reason: string,
    author: string,
  ) => Promise<void>;
}) {
  const [target, setTarget] = useState("");
  const [confirmKey, setConfirmKey] = useState<string | null>(null);
  const { reason, author, setReason, setAuthor } = fields;
  const candidates = allTasks.filter((t) => t.id !== task.id && !task.depends_on.includes(t.key));
  const ready = reason.trim() !== "" && author.trim() !== "";
  const byKey = new Map(allTasks.map((t) => [t.key, t]));
  const sourceOf = new Map(task.dependencies.map((d) => [d.key, d.source]));

  return (
    <div
      role="group"
      aria-label={`Edit dependencies for ${task.key}`}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onClose();
        }
      }}
    >
      <div className="stack">
        <label>
          Depends on
          <select
            aria-label={`Dependency for ${task.key}`}
            value={target}
            onChange={(e) => setTarget(e.target.value)}
          >
            <option value="">Select a task…</option>
            {candidates.map((t) => (
              <option
                key={t.id}
                value={t.id}
                disabled={wouldCycle(allTasks, task, t)}
                title={wouldCycle(allTasks, task, t) ? "Would create a cycle" : undefined}
              >
                {t.key} · {t.title}
              </option>
            ))}
          </select>
        </label>
        <label>
          Reason
          <input
            aria-label={`Reason for ${task.key}`}
            maxLength={500}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </label>
        <label>
          Your name
          <input
            aria-label={`Author for ${task.key}`}
            maxLength={100}
            value={author}
            onChange={(e) => setAuthor(e.target.value)}
          />
        </label>
        <div className="btn-row">
          <button
            type="button"
            disabled={!target || !ready}
            onClick={() => {
              const chosen = allTasks.find((t) => t.id === target);
              if (chosen) void onEdit("add", task, chosen, reason, author).then(() => setTarget(""));
            }}
          >
            Add
          </button>
          {task.depends_on.map((key) => {
            const other = byKey.get(key);
            const derived = sourceOf.get(key) === "diagram" || sourceOf.get(key) === "nfr rule";
            return (
              <button
                key={key}
                type="button"
                disabled={!ready || !other}
                onClick={() => {
                  if (!other) return;
                  if (derived) setConfirmKey(key);
                  else void onEdit("remove", task, other, reason, author);
                }}
              >
                {derived ? `Remove derived dependency ${key}` : `Remove ${key}`}
              </button>
            );
          })}
          <button type="button" onClick={onClose}>
            Cancel
          </button>
        </div>
        {confirmKey && (
          <div role="alert">
            <p>The next Rebuild will restore {confirmKey}. Remove it anyway?</p>
            <div className="btn-row">
              <button
                type="button"
                onClick={() => {
                  const other = byKey.get(confirmKey);
                  setConfirmKey(null);
                  if (other) void onEdit("remove", task, other, reason, author);
                }}
              >
                Confirm removal
              </button>
              <button type="button" onClick={() => setConfirmKey(null)}>
                Keep dependency
              </button>
            </div>
          </div>
        )}
        <p className="hint">Add and remove need a reason and your name; they are recorded.</p>
      </div>
    </div>
  );
}

function TaskCard({
  task,
  titles,
  allTasks,
  editable,
  editing,
  onToggleEditor,
  fields,
  open,
  onToggleSection,
  onEdit,
}: {
  task: PlanTask;
  titles: Map<string, string>;
  allTasks: PlanTask[];
  editable: boolean;
  editing: boolean;
  onToggleEditor: (open: boolean) => void;
  fields: EditorFields;
  open: Set<string>;
  onToggleSection: (key: string, isOpen: boolean) => void;
  onEdit: (
    action: "add" | "remove",
    task: PlanTask,
    target: PlanTask,
    reason: string,
    author: string,
  ) => Promise<void>;
}) {
  const editButton = useRef<HTMLButtonElement>(null);
  const reqKey = `${task.id}:req`;
  const acKey = `${task.id}:ac`;
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
        {task.dependencies.length === 0
          ? "none"
          : task.dependencies.map((d, i) => (
              <span key={d.key}>
                {i > 0 && ", "}
                {d.key}
                {d.source === "nfr rule" && (
                  <>
                    {" "}
                    <span className="badge badge-type">derived: nfr rule</span>
                  </>
                )}
                {d.source === "manual" && (
                  <>
                    {" "}
                    <span
                      className="badge badge-warn"
                      title={`${d.reason ?? ""}${d.author ? ` (${d.author})` : ""}`}
                    >
                      edited
                    </span>
                  </>
                )}
              </span>
            ))}
      </p>
      {editable && (
        <>
          <button
            type="button"
            ref={editButton}
            aria-expanded={editing}
            onClick={() => onToggleEditor(!editing)}
          >
            Edit dependencies
          </button>
          {editing && (
            <DependencyEditor
              task={task}
              allTasks={allTasks}
              fields={fields}
              onEdit={onEdit}
              onClose={() => {
                onToggleEditor(false);
                editButton.current?.focus();
              }}
            />
          )}
        </>
      )}
      {task.notes.length > 0 && (
        <ul aria-label={`Dependency notes for ${task.key}`}>
          {task.notes.map((n, i) => (
            <li key={i}>
              <span className="badge badge-warn">Unmapped dependency</span>{" "}
              {n.replace(/^Unmapped dependency:\s*/, "")}
            </li>
          ))}
        </ul>
      )}
      {task.related.length > 0 && (
        <p className="muted">Related: {task.related.join(", ")}</p>
      )}
      <details
        open={open.has(reqKey)}
        onToggle={(e) => onToggleSection(reqKey, e.currentTarget.open)}
      >
        <summary>Linked requirements ({task.requirement_ids.length})</summary>
        {task.requirement_ids.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <ul aria-label={`Requirements for ${task.key}`}>
            {task.requirement_ids.map((id) => (
              <li key={id}>{titles.get(id) ?? id}</li>
            ))}
          </ul>
        )}
      </details>
      <details open={open.has(acKey)} onToggle={(e) => onToggleSection(acKey, e.currentTarget.open)}>
        <summary>Acceptance criteria ({task.acceptance_criteria.length})</summary>
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
      </details>
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
  const [editingId, setEditingId] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [author, setAuthor] = useState("");
  const [openSections, setOpenSections] = useState<Set<string>>(new Set());
  const [showOlder, setShowOlder] = useState(false);
  const fields: EditorFields = { reason, author, setReason, setAuthor };
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
        const summary: PlanSummary = {
          id: plan.id,
          project_id: plan.project_id,
          proposal_id: plan.proposal_id,
          version: plan.version,
          status: plan.status,
          generated_at: plan.generated_at,
          source_graph_built_at: plan.source_graph_built_at,
          task_count: plan.tasks.length,
        };
        const rest = (cur ?? []).filter((s) => s.id !== plan.id);
        return [summary, ...rest].sort((a, b) => b.version - a.version);
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : fallback);
    } finally {
      setBusy(false);
    }
  }

  async function edit(
    action: "add" | "remove",
    task: PlanTask,
    target: PlanTask,
    reason: string,
    author: string,
  ) {
    if (!selected) return;
    const planId = selected.id;
    await act(
      () =>
        editPlanDependency(projectId, planId, action, {
          task_id: task.id,
          depends_on_task_id: target.id,
          reason: reason.trim(),
          author: author.trim(),
        }),
      "Dependency edit failed",
    );
  }

  const draft = selected?.status === "draft";
  const unmappedNotes = selected
    ? selected.tasks.reduce((n, t) => n + t.notes.length, 0)
    : 0;
  const sectionKeys = (selected?.tasks ?? []).flatMap((t) => [`${t.id}:req`, `${t.id}:ac`]);
  const allOpen = sectionKeys.length > 0 && sectionKeys.every((k) => openSections.has(k));
  const sortedSummaries = [...(summaries ?? [])].sort((a, b) => b.version - a.version);
  const latestIds = new Set(sortedSummaries.slice(0, 3).map((s) => s.id));
  const isVisible = (s: PlanSummary) =>
    latestIds.has(s.id) || s.status !== "draft" || s.id === selected?.id;
  const older = sortedSummaries.filter((s) => !isVisible(s));
  const shownVersions = showOlder ? sortedSummaries : sortedSummaries.filter(isVisible);
  const description = projectDescription ? stripTrailingHeading(projectDescription) : projectDescription;

  return (
    <section aria-label="Plan" className="stack">
      <ProjectHeader
        projectId={projectId}
        name={projectName}
        description={description}
        proposals={proposals}
        confirmedCount={confirmedCount}
        plans={summaries ?? undefined}
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
              {shownVersions.map((s) => (
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
              {older.length > 0 && (
                <li>
                  <button
                    type="button"
                    className="tab"
                    aria-expanded={showOlder}
                    onClick={() => setShowOlder((v) => !v)}
                  >
                    {showOlder ? "Hide older drafts" : `Show ${older.length} older drafts`}
                  </button>
                </li>
              )}
            </ul>
            <div className="title-row">
              <h2>Plan version {selected.version}</h2>
              <StatusBadge status={selected.status} testId="plan-status" />
            </div>
            <p className="sub">
              {selected.tasks.length} task(s) in implementation order ·{" "}
              {selected.dependency_count} dependencies derived · {selected.unmapped_node_count}{" "}
              diagram node(s) unmapped
            </p>
            {selected.warnings.length > 0 && (
              <ul aria-label="Plan warnings">
                {selected.warnings.map((w, i) => (
                  <li key={i} role="alert">
                    <span className="badge badge-warn">Warning</span> {w}
                  </li>
                ))}
              </ul>
            )}
            {draft && unmappedNotes > 0 && (
              <p role="alert" data-testid="unmapped-banner">
                <span className="badge badge-warn">Unmapped dependency</span> {unmappedNotes}{" "}
                unmapped dependency note(s) remain. Approving keeps the plan order as derived from
                the mapped components only.
              </p>
            )}
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

          {selected.previous_manual_edits.length > 0 && (
            <div className="card" role="note">
              <h3>Manual edits of version {selected.previous_version} (not carried over)</h3>
              <p className="sub">
                A rebuild never copies manual dependency edits. Re-apply any that still matter.
              </p>
              <ul aria-label="Previous manual edits">
                {selected.previous_manual_edits.map((e, i) => (
                  <li key={i}>
                    {e.action === "add" ? "Added" : "Removed"} {e.task_key} → {e.depends_on_key}:{" "}
                    {e.reason} ({e.author})
                  </li>
                ))}
              </ul>
            </div>
          )}

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

          {selected.not_for_client.length > 0 && (
            <div className="card">
              <h3>Not for the client</h3>
              <p className="sub">
                Questions about the proposal itself, not about the project. They block no task.
              </p>
              <ul aria-label="Not for the client">
                {selected.not_for_client.map((q, i) => (
                  <li key={i}>{q}</li>
                ))}
              </ul>
            </div>
          )}

          <div className="btn-row">
            <button
              type="button"
              onClick={() => setOpenSections(allOpen ? new Set() : new Set(sectionKeys))}
            >
              {allOpen ? "Collapse all" : "Expand all"}
            </button>
          </div>
          <ol className="stack" aria-label="Plan tasks">
            {selected.tasks.map((t) => (
              <TaskCard
                key={t.id}
                task={t}
                titles={titles}
                allTasks={selected.tasks}
                editable={!!draft}
                editing={editingId === t.id}
                onToggleEditor={(o) => setEditingId(o ? t.id : null)}
                fields={fields}
                open={openSections}
                onToggleSection={(k, o) =>
                  setOpenSections((cur) => {
                    if (cur.has(k) === o) return cur;
                    const next = new Set(cur);
                    if (o) next.add(k);
                    else next.delete(k);
                    return next;
                  })
                }
                onEdit={edit}
              />
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}
