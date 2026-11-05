import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  ApiError,
  analyseChange,
  approveChangeArchitecture,
  createChange,
  createPlanDelta,
  decideChange,
  getChange,
  listChanges,
  overrideChangeClass,
  proposeArchitectureUpdate,
  proposeChangeArchitecture,
  runChange,
  type ChangeClass,
  type ChangeDetail,
  type ChangeSummary,
  type DiffItem,
} from "./api";
import { hrefFor } from "./routes";
import { EmptyState, ErrorState, LoadingState, StatusBadge } from "./ui";

const CLASS_LABEL: Record<ChangeClass, string> = {
  code_only: "Code only",
  architecture: "Architecture",
};

const message = (e: unknown) =>
  e instanceof ApiError || e instanceof Error ? e.message : "Request failed";

function DiffList({ title, items }: { title: string; items: DiffItem[] }) {
  if (items.length === 0) return null;
  return (
    <li>
      {title}: {items.map((i) => i.name).join(", ")}
    </li>
  );
}

function ChangeView({
  projectId,
  change,
  onChange,
}: {
  projectId: string;
  change: ChangeDetail;
  onChange: (c: ChangeDetail | null) => void;
}) {
  const [by, setBy] = useState("");
  const [reason, setReason] = useState("");
  const [removalReason, setRemovalReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const act = async (fn: () => Promise<ChangeDetail | unknown>) => {
    setBusy(true);
    setError(null);
    try {
      const result = await fn();
      onChange(
        result && typeof result === "object" && "log" in result
          ? (result as ChangeDetail)
          : await getChange(projectId, change.id),
      );
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };

  const effective = change.effective_class;
  const draft = change.proposals.find((p) => p.status === "draft");
  const approvedDraft = change.proposals.find((p) => p.status === "approved");
  const person = by.trim();
  const open = change.status === "draft" || change.status === "analysed";
  const decided = change.status === "approved" || change.status === "in_progress";
  const removals = draft?.diff?.removals ?? [];

  return (
    <article aria-label={`Change ${change.key}`} className="stack">
      <h2>
        {change.key} <StatusBadge status={change.status} />
      </h2>
      <p>{change.text}</p>
      <p className="muted">By {change.author}</p>
      {error && <ErrorState message={error} />}

      {change.drift_flagged && (
        <div role="alert">
          <p>
            Architecture drift: the accepted patch added something the approved
            architecture does not mention.
          </p>
          <ul>
            {change.drift.map((d) => (
              <li key={`${d.kind}:${d.name}`}>{d.detail}</li>
            ))}
          </ul>
          <button
            type="button"
            disabled={busy || !person}
            onClick={() =>
              void act(() => proposeArchitectureUpdate(projectId, change.id, person))
            }
          >
            Propose architecture update
          </button>
        </div>
      )}

      {change.impact && (
        <section aria-label="Impact">
          <h3>Impact</h3>
          <p>
            <strong>{effective ? CLASS_LABEL[effective] : "Unclassified"}</strong>
            {change.class_override && (
              <>
                {" "}
                (overridden by {change.overridden_by}: {change.override_reason};
                analysis said {CLASS_LABEL[change.impact.impact_class]})
              </>
            )}
          </p>
          <p>{change.impact.reason}</p>
          <ul>
            {change.impact.components.length > 0 && (
              <li>
                Components: {change.impact.components.map((c) => c.name).join(", ")}
              </li>
            )}
            {change.impact.decisions.length > 0 && (
              <li>
                Decisions: {change.impact.decisions.map((d) => d.name).join(", ")}
              </li>
            )}
            {change.impact.requirements.length > 0 && (
              <li>
                Requirements:{" "}
                {change.impact.requirements.map((r) => r.title).join(", ")}
              </li>
            )}
          </ul>
        </section>
      )}

      {(open || decided) && (
        <div className="row">
          <label>
            Your name
            <input value={by} onChange={(e) => setBy(e.target.value)} />
          </label>
          <label>
            Reason
            <input value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
        </div>
      )}

      {open && change.impact && (
        <div className="row">
          <button
            type="button"
            disabled={busy || !person || !reason.trim()}
            onClick={() =>
              void act(() =>
                overrideChangeClass(projectId, change.id, {
                  impact_class:
                    effective === "architecture" ? "code_only" : "architecture",
                  by: person,
                  reason: reason.trim(),
                }),
              )
            }
          >
            {effective === "architecture"
              ? "Treat as code only"
              : "Treat as architecture"}
          </button>
        </div>
      )}
      {change.status === "draft" && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void act(() => analyseChange(projectId, change.id))}
        >
          Analyse impact
        </button>
      )}
      {change.status === "analysed" && (
        <div className="row">
          <button
            type="button"
            disabled={busy || !person}
            onClick={() =>
              void act(() =>
                decideChange(projectId, change.id, "approve", {
                  by: person,
                  reason: reason.trim() || undefined,
                }),
              )
            }
          >
            Approve request
          </button>
          <button
            type="button"
            disabled={busy || !person || !reason.trim()}
            onClick={() =>
              void act(() =>
                decideChange(projectId, change.id, "reject", {
                  by: person,
                  reason: reason.trim(),
                }),
              )
            }
          >
            Reject request
          </button>
        </div>
      )}

      {decided && effective === "code_only" && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void act(() => runChange(projectId, change.id))}
        >
          Run agent on this request
        </button>
      )}
      {decided && effective === "architecture" && !draft && !approvedDraft && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void act(() => proposeChangeArchitecture(projectId, change.id))}
        >
          Propose architecture version
        </button>
      )}

      {change.proposals.map((p) => (
        <section key={p.id} aria-label={`Architecture version ${p.version}`}>
          <h3>
            Architecture v{p.version} <StatusBadge status={p.status} />
          </h3>
          {p.diff && (
            <ul>
              <DiffList title="Components added" items={p.diff.components.added} />
              <DiffList title="Components changed" items={p.diff.components.changed} />
              <DiffList title="Components removed" items={p.diff.components.removed} />
              <DiffList title="Decisions added" items={p.diff.decisions.added} />
              <DiffList title="Decisions changed" items={p.diff.decisions.changed} />
              <DiffList title="Decisions removed" items={p.diff.decisions.removed} />
            </ul>
          )}
        </section>
      ))}

      {draft && (
        <div className="stack">
          {removals.length > 0 && (
            <label>
              Reason for removing {removals.map((r) => r.name).join(", ")}
              <input
                value={removalReason}
                onChange={(e) => setRemovalReason(e.target.value)}
              />
            </label>
          )}
          <button
            type="button"
            disabled={busy || (removals.length > 0 && !removalReason.trim())}
            onClick={() =>
              void act(() =>
                approveChangeArchitecture(
                  projectId,
                  change.id,
                  removals.map((r) => ({
                    kind: r.kind,
                    key: r.key,
                    reason: removalReason.trim(),
                  })),
                ),
              )
            }
          >
            Approve architecture version
          </button>
        </div>
      )}
      {approvedDraft && change.log.plan_deltas.length === 0 && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void act(() => createPlanDelta(projectId, change.id))}
        >
          Create plan delta
        </button>
      )}

      <section aria-label="Change log">
        <h3>Change log</h3>
        <ul>
          {change.log.architecture.map((a) => (
            <li key={a.id}>
              <a href={hrefFor("architecture", projectId)}>
                Architecture v{a.version}
              </a>{" "}
              ({a.status}
              {a.parent_version !== null ? `, from v${a.parent_version}` : ""})
            </li>
          ))}
          {change.log.plan_deltas.map((p) => (
            <li key={p.id}>
              <a href={hrefFor("plan", projectId)}>Plan delta v{p.version}</a> (
              {p.status})
            </li>
          ))}
          {change.log.runs.map((r) => (
            <li key={r.id}>
              Run {r.task_key} ({r.status})
            </li>
          ))}
          {change.log.patches.map((p) => (
            <li key={p.id}>
              Patch {p.task_key} ({p.status})
            </li>
          ))}
          {change.log.ci_runs.map((c) => (
            <li key={c.run_id}>CI run for {c.run_id}</li>
          ))}
          {change.log.pull_request_url && (
            <li>
              <a href={change.log.pull_request_url}>Pull request</a>
            </li>
          )}
          {change.log.requirement_ids.length > 0 && (
            <li>
              <a href={hrefFor("requirements", projectId)}>
                {change.log.requirement_ids.length} proposed requirement(s)
              </a>
            </li>
          )}
        </ul>
      </section>
    </article>
  );
}

export default function ChangesSection({ projectId }: { projectId: string }) {
  const [changes, setChanges] = useState<ChangeSummary[] | null>(null);
  const [selected, setSelected] = useState<ChangeDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [author, setAuthor] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setChanges(await listChanges(projectId));
      setError(null);
    } catch (e) {
      setError(message(e));
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await createChange(projectId, text.trim(), author.trim());
      setSelected(await analyseChange(projectId, created.id));
      setText("");
      await load();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };

  const open = async (id: string) => {
    try {
      setSelected(await getChange(projectId, id));
    } catch (e) {
      setError(message(e));
    }
  };

  return (
    <div className="stack">
      <form onSubmit={(e) => void submit(e)} className="stack">
        <label>
          Describe the change
          <textarea
            value={text}
            maxLength={4000}
            onChange={(e) => setText(e.target.value)}
            rows={3}
          />
        </label>
        <label>
          Your name
          <input value={author} onChange={(e) => setAuthor(e.target.value)} />
        </label>
        <button type="submit" disabled={busy || !text.trim() || !author.trim()}>
          Submit change request
        </button>
      </form>
      {error && <ErrorState message={error} onRetry={() => void load()} />}
      {changes === null && !error && <LoadingState label="Loading changes…" />}
      {changes !== null && changes.length === 0 && (
        <EmptyState>
          <p>No change requests yet.</p>
        </EmptyState>
      )}
      {changes !== null && changes.length > 0 && (
        <ul aria-label="Change requests">
          {changes.map((c) => (
            <li key={c.id}>
              <button type="button" onClick={() => void open(c.id)}>
                {c.key}: {c.text.slice(0, 80)}
              </button>{" "}
              <StatusBadge status={c.status} />{" "}
              {c.effective_class && <span>{CLASS_LABEL[c.effective_class]}</span>}
              {c.drift_flagged && <span> · drift</span>}
            </li>
          ))}
        </ul>
      )}
      {selected && (
        <ChangeView
          key={selected.id}
          projectId={projectId}
          change={selected}
          onChange={(c) => {
            if (c) setSelected(c);
            void load();
          }}
        />
      )}
    </div>
  );
}
