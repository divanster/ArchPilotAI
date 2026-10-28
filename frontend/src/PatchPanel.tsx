import { useCallback, useEffect, useState } from "react";
import AgentRunPanel from "./AgentRunPanel";
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
} from "./api";

const AI_FLAG_PREFIX = "AI recommendation";

function errText(e: unknown, fallback: string): string {
  return e instanceof Error ? e.message : fallback;
}

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
      }
    } finally {
      setBusy(false);
    }
  };

  const generate = () =>
    run(async () => {
      const made = await generatePatch(projectId, planId, task.key, ack);
      setDetail(made);
      await refresh();
      onChanged();
    }, "Patch generation failed");

  const open = (id: string) =>
    run(
      async () => setDetail(await getPatch(projectId, id)),
      "Could not load patch",
    );

  const review = (action: "accept" | "reject") =>
    run(async () => {
      if (!detail) return;
      const done = await reviewPatch(projectId, detail.id, action, {
        reviewer: reviewer.trim(),
        reason: reason.trim(),
      });
      setDetail(done);
      setReason("");
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
            </li>
          ))}
        </ul>
      )}
      {detail && (
        <div aria-label={`Patch v${detail.version} of ${task.key}`}>
          <p>
            Version {detail.version} · <strong>{detail.status}</strong> ·{" "}
            {detail.provider}/{detail.model}
          </p>
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
          {detail.files.map((f) => (
            <details key={f.path} open>
              <summary>
                {f.change} {f.path}
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
                  disabled={busy || !canSubmitReview}
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
