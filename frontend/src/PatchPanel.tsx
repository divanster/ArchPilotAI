import { useCallback, useEffect, useState } from "react";
import AgentRunPanel from "./AgentRunPanel";
import GitPatchActions from "./GitPatchActions";
import {
  ApiError,
  confirmTechnology,
  generatePatch,
  getPatch,
  listPatches,
  reviewPatch,
  type CodePatchDetail,
  type PatchSummary,
  type PlanTask,
  type RemovalsNotConfirmedDetail,
} from "./api";

const AI_FLAG_PREFIX = "AI recommendation";

function errText(e: unknown, fallback: string): string {
  return e instanceof Error ? e.message : fallback;
}

const removalKey = (r: { file: string; name: string }) =>
  `${r.file}\u0000${r.name}`;

export default function PatchPanel({
  projectId,
  planId,
  task,
  approved,
  onChanged,
}: {
  projectId: string;
  planId: string;
  task: PlanTask;
  approved: boolean;
  onChanged: () => void;
}) {
  const [patches, setPatches] = useState<PatchSummary[]>([]);
  const [detail, setDetail] = useState<CodePatchDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ack, setAck] = useState(false);
  const [techName, setTechName] = useState("");
  const [techReason, setTechReason] = useState("");
  const [techDone, setTechDone] = useState(false);
  const [reviewer, setReviewer] = useState("");
  const [reason, setReason] = useState("");
  const [removalState, setRemovalState] = useState<
    Record<string, { confirmed: boolean; reason: string }>
  >({});
  const [unconfirmed, setUnconfirmed] = useState<
    { file: string; name: string }[]
  >([]);

  const needsTech = task.flags.some((f) => f.startsWith(AI_FLAG_PREFIX));

  const refresh = useCallback(async () => {
    const list = await listPatches(projectId, planId, task.key);
    setPatches([...list].sort((a, b) => b.version - a.version));
    return list;
  }, [projectId, planId, task.key]);

  useEffect(() => {
    if (!approved) return;
    void refresh().catch(() => undefined);
  }, [approved, refresh]);

  if (!approved) return null;

  const removals = detail?.needs_justification
    ? (detail.removed_from_accepted ?? [])
    : [];
  const showDetail = (d: CodePatchDetail) => {
    setDetail(d);
    setRemovalState({});
    setUnconfirmed([]);
  };
  const run = async (fn: () => Promise<void>, fallback: string) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errText(e, fallback));
      if (e instanceof ApiError && e.status === 409) {
        const d = e.detail as { code?: string } | null;
        if (d?.code === "technology_unconfirmed") setTechDone(false);
        const r = e.detail as Partial<RemovalsNotConfirmedDetail> | null;
        if (r?.code === "removals_not_confirmed") {
          setUnconfirmed(Array.isArray(r.missing) ? r.missing : []);
        }
      }
    } finally {
      setBusy(false);
    }
  };

  const generate = () =>
    run(async () => {
      const made = await generatePatch(projectId, planId, task.key, ack);
      setDetail(made);
      setRemovalState({});
      setUnconfirmed([]);
      await refresh();
      onChanged();
    }, "Patch generation failed");

  const open = (id: string) =>
    run(
      async () => showDetail(await getPatch(projectId, id)),
      "Could not load patch",
    );

  const review = (action: "accept" | "reject") =>
    run(async () => {
      if (!detail) return;
      setUnconfirmed([]);
      const body: Parameters<typeof reviewPatch>[3] = {
        reviewer: reviewer.trim(),
        reason: reason.trim(),
      };
      if (action === "accept" && removals.length > 0) {
        body.confirmed_removals = removals.map((r) => ({
          file: r.file,
          name: r.name,
          reason: (removalState[removalKey(r)]?.reason ?? "").trim(),
        }));
      }
      const done = await reviewPatch(projectId, detail.id, action, body);
      setDetail(done);
      setReason("");
      setRemovalState({});
      await refresh();
      onChanged();
    }, "Review failed");

  const confirmTech = () =>
    run(async () => {
      await confirmTechnology(projectId, planId, task.key, {
        name: techName.trim(),
        reason: techReason.trim(),
      });
      setTechDone(true);
    }, "Technology confirmation failed");

  const canReview = detail?.status === "proposed";
  const canSubmitReview = reviewer.trim() !== "" && reason.trim() !== "";
  const allRemovalsConfirmed = removals.every((r) => {
    const s = removalState[removalKey(r)];
    return !!s?.confirmed && s.reason.trim() !== "";
  });

  return (
    <div data-testid={`patches-${task.key}`}>
      <h4>Code patch</h4>
      {needsTech && (
        <fieldset disabled={busy}>
          <legend>Confirm technology</legend>
          <label>
            Technology name
            <input
              value={techName}
              onChange={(e) => setTechName(e.target.value)}
            />
          </label>
          <label>
            Reason
            <input
              value={techReason}
              onChange={(e) => setTechReason(e.target.value)}
            />
          </label>
          <button
            type="button"
            disabled={!techName.trim() || !techReason.trim()}
            onClick={() => void confirmTech()}
          >
            Record confirmation
          </button>
          {techDone && <span role="status">Technology confirmed.</span>}
        </fieldset>
      )}
      {task.blocking_questions.length > 0 && (
        <label>
          <input
            type="checkbox"
            checked={ack}
            onChange={(e) => setAck(e.target.checked)}
          />
          Acknowledge blocking questions for this generation
        </label>
      )}
      <div className="btn-row">
        <button type="button" disabled={busy} onClick={() => void generate()}>
          {patches.length > 0 ? "Regenerate patch" : "Generate patch"}
        </button>
      </div>
      <AgentRunPanel
        projectId={projectId}
        planId={planId}
        task={task}
        acknowledge={ack}
        disabled={busy}
        onOpenPatch={(id) => {
          void open(id);
          void refresh().catch(() => undefined);
          onChanged();
        }}
      />
      {error && <p role="alert">{error}</p>}
      {patches.length > 0 && (
        <ul aria-label={`Patch versions for ${task.key}`}>
          {patches.map((p) => (
            <li key={p.id}>
              <button type="button" onClick={() => void open(p.id)}>
                v{p.version}
              </button>{" "}
              <span className="badge">{p.status}</span> · {p.file_count} file(s)
              {p.based_on_version != null && (
                <span className="muted">
                  {" "}
                  · Based on feedback from version {p.based_on_version}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {detail && (
        <div aria-label={`Patch v${detail.version} of ${task.key}`}>
          <p>
            Version {detail.version} · <strong>{detail.status}</strong> ·{" "}
            {detail.provider}/{detail.model} · {detail.files.length} file(s)
          </p>
          {detail.based_on_version != null && (
            <p className="hint">
              Based on feedback from version {detail.based_on_version}
            </p>
          )}
          {detail.coverage && detail.coverage.items.length > 0 && (
            <div>
              <strong>
                {detail.coverage.covered} covered · {detail.coverage.missing}{" "}
                missing · {detail.coverage.unchecked} unchecked
              </strong>
              <ul aria-label="Acceptance coverage">
                {detail.coverage.items.map((c, i) => (
                  <li key={i}>
                    <span
                      className={
                        c.status === "covered" ? "badge" : "badge badge-warn"
                      }
                    >
                      {c.status}
                    </span>{" "}
                    {c.ref}: {c.text}
                    {c.missing.length > 0 && (
                      <> (missing: {c.missing.join(", ")})</>
                    )}
                    {c.status === "unchecked" && (
                      <>
                        {" "}
                        — {c.reason ?? "no key terms extracted"}:{" "}
                        {c.prompt ?? "check manually"}
                      </>
                    )}
                  </li>
                ))}
              </ul>
              {detail.coverage.removed && (
                <div>
                  <strong>
                    Removed since v{detail.coverage.removed.since}
                  </strong>
                  <ul aria-label="Removed since previous version">
                    {detail.coverage.removed.items.map((r, i) => (
                      <li key={i}>{r}</li>
                    ))}
                  </ul>
                </div>
              )}
              {detail.coverage.scope && detail.coverage.scope.length > 0 && (
                <ul aria-label="Scope notes">
                  {detail.coverage.scope.map((s, i) => (
                    <li key={i}>{s}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
          {detail.coverage?.affected && (
            <div>
              <strong>Affected requirements and tasks</strong>
              {detail.coverage.affected.note && (
                <p>{detail.coverage.affected.note}</p>
              )}
              <ul aria-label="Affected requirements and tasks">
                {detail.coverage.affected.requirements.map((r) => (
                  <li key={`r-${r.id}`}>Requirement: {r.title}</li>
                ))}
                {detail.coverage.affected.tasks.map((t) => (
                  <li key={`t-${t.key}`}>
                    Task {t.key}: {t.title}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {detail.validation === "stale" && (
            <p role="alert">
              Stale patch: the workspace changed since generation (
              {detail.stale
                .map((s) => `${s.code} ${s.path ?? ""}`.trim())
                .join(", ")}
              ). Regenerate it.
            </p>
          )}
          {detail.warnings.length > 0 && (
            <ul aria-label="Patch warnings">
              {detail.warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          )}
          {removals.length > 0 && (
            <section
              aria-label="Removed from accepted file"
              style={{
                border: "1px solid #c0392b",
                color: "#c0392b",
                padding: "0.5rem",
              }}
            >
              <strong>Removed from accepted file ({removals.length})</strong>
              {unconfirmed.length > 0 && (
                <p role="alert">
                  Unconfirmed removals:{" "}
                  {unconfirmed.map((u) => `${u.name} (${u.file})`).join(", ")}
                </p>
              )}
              <ul>
                {removals.map((r) => {
                  const key = removalKey(r);
                  const s = removalState[key] ?? {
                    confirmed: false,
                    reason: "",
                  };
                  const set = (p: Partial<typeof s>) =>
                    setRemovalState((m) => ({ ...m, [key]: { ...s, ...p } }));
                  const label = `${r.kind} ${r.name} in ${r.file}`;
                  return (
                    <li key={key}>
                      {r.kind} <strong>{r.name}</strong> · {r.file}{" "}
                      <span className={r.listed ? "badge" : "badge badge-warn"}>
                        {r.listed ? "listed" : "unintended"}
                      </span>{" "}
                      <label>
                        <input
                          type="checkbox"
                          aria-label={`Confirm removal of ${label}`}
                          checked={s.confirmed}
                          onChange={(e) => set({ confirmed: e.target.checked })}
                        />
                        Confirm
                      </label>{" "}
                      <input
                        aria-label={`Removal reason for ${label}`}
                        placeholder="Reason"
                        value={s.reason}
                        onChange={(e) => set({ reason: e.target.value })}
                      />
                    </li>
                  );
                })}
              </ul>
            </section>
          )}
          {detail.files.map((f) => (
            <details key={f.path} open>
              <summary>
                {f.change === "create"
                  ? "new"
                  : f.change === "modify"
                    ? "modified"
                    : "deleted"}{" "}
                ({f.change}) {f.path}
              </summary>
              <pre>{f.diff}</pre>
              {f.diff_truncated && <p className="hint">Diff truncated.</p>}
            </details>
          ))}
          {(
            [
              ["Notes", detail.notes],
              ["Assumptions", detail.assumptions],
              ["Test suggestions", detail.test_suggestions],
            ] as const
          ).map(([title, items]) =>
            items.length > 0 ? (
              <div key={title}>
                <strong>{title}</strong>
                <ul aria-label={title}>
                  {items.map((n, i) => (
                    <li key={i}>{n}</li>
                  ))}
                </ul>
              </div>
            ) : null,
          )}
          {detail.reviewed_by && (
            <p>
              Reviewed by {detail.reviewed_by}: {detail.review_reason}
            </p>
          )}
          {detail.status === "accepted" && (
            <GitPatchActions
              key={detail.id}
              projectId={projectId}
              patchId={detail.id}
              reviewer={detail.reviewed_by ?? null}
            />
          )}
          {canReview && (
            <div>
              <label>
                Reviewer
                <input
                  value={reviewer}
                  onChange={(e) => setReviewer(e.target.value)}
                />
              </label>
              <label>
                Review reason
                <input
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>
              <div className="btn-row">
                <button
                  type="button"
                  className="approve"
                  disabled={busy || !canSubmitReview || !allRemovalsConfirmed}
                  onClick={() => void review("accept")}
                >
                  Accept patch
                </button>
                <button
                  type="button"
                  className="danger"
                  disabled={busy || !canSubmitReview}
                  onClick={() => void review("reject")}
                >
                  Reject patch
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
