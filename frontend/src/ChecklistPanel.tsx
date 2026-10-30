import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  agentReviseChecklist,
  buildChecklist,
  decideChecklist,
  getChecklist,
  resumeStep,
  reviseChecklist,
  runNextStep,
  type ChecklistStatus,
  type ChecklistStep,
  type ChecklistView,
  type PlanTask,
} from "./api";

const POLL_MS = 1500;

interface Problem {
  code: string;
  step: string | null;
  message: string;
}

function problemsOf(e: unknown): Problem[] {
  if (!(e instanceof ApiError)) return [];
  const d = e.detail as { problems?: unknown } | null;
  return d && Array.isArray(d.problems) ? (d.problems as Problem[]) : [];
}

function parseKeys(text: string): string[] {
  return text
    .split(",")
    .map((k) => k.trim())
    .filter((k) => k !== "");
}

function StepStatus({ step }: { step: ChecklistStep }) {
  return (
    <span className="sub" aria-label={`Status of ${step.key}`}>
      {" "}
      <span className="badge">{step.status}</span>
      {step.patch ? ` · patch v${step.patch.version} ${step.patch.status}` : ""}
      {step.checks.tests ? ` · tests ${step.checks.tests}` : ""}
      {step.checks.finding_total !== null
        ? ` · ${step.checks.finding_total} finding(s)`
        : ""}
      {step.run?.stop_reason && step.run.status !== "finished"
        ? ` · ${step.run.stop_reason}`
        : ""}
      {step.run?.resumed_from_run_id ? " · resumed" : ""}
      {step.resumable ? " · checkpoint saved" : ""}
    </span>
  );
}

export default function ChecklistPanel({
  projectId,
  planId,
  task,
  acknowledge,
  disabled,
  refreshKey = 0,
  onOpenPatch,
  onChanged,
  onStatusChange,
}: {
  projectId: string;
  planId: string;
  task: PlanTask;
  acknowledge: boolean;
  disabled: boolean;
  refreshKey?: number;
  onOpenPatch: (patchId: string) => void;
  onChanged?: () => void;
  /** The status of the current checklist version (null: no checklist). */
  onStatusChange?: (status: ChecklistStatus | null) => void;
}) {
  const [view, setView] = useState<ChecklistView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [problems, setProblems] = useState<Problem[]>([]);
  const [name, setName] = useState("");
  const [reason, setReason] = useState("");
  const [instruction, setInstruction] = useState("");
  // Unsaved dependency edits of one checklist version: step key -> comma-separated keys.
  const [edits, setEdits] = useState<{
    id: string | null;
    depends: Record<string, string>;
  }>({ id: null, depends: {} });
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    try {
      const next = await getChecklist(projectId, planId, task.key);
      if (mounted.current && next && "checklist" in next) setView(next);
    } catch {
      // A missing checklist endpoint leaves the panel empty; task-level runs still work.
    }
  }, [projectId, planId, task.key]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const checklist = view?.checklist ?? null;
  const steps = checklist?.steps ?? [];
  const status = view ? (checklist?.status ?? null) : undefined;

  useEffect(() => {
    if (status !== undefined) onStatusChange?.(status);
  }, [status, onStatusChange]);
  const running = steps.some((s) => s.status === "running");

  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [running, load]);

  const edited = checklist && edits.id === checklist.id ? edits.depends : {};
  const dependsText = (s: ChecklistStep) =>
    edited[s.key] ?? s.depends_on.join(", ");
  const editDepends = (key: string, value: string) =>
    setEdits({
      id: checklist?.id ?? null,
      depends: { ...edited, [key]: value },
    });

  const act = async (fn: () => Promise<unknown>, fallback: string) => {
    setBusy(true);
    setError(null);
    setProblems([]);
    try {
      await fn();
      await load();
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : fallback);
      setProblems(problemsOf(e));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  const propose = () =>
    act(async () => {
      setView(await buildChecklist(projectId, planId, task.key));
    }, "Could not propose a checklist");

  const agentRevise = () =>
    act(async () => {
      setView(
        await agentReviseChecklist(projectId, planId, task.key, instruction),
      );
      setInstruction("");
    }, "The agent revision failed");

  const saveRevision = () =>
    act(async () => {
      setView(
        await reviseChecklist(projectId, planId, task.key, {
          author: name.trim(),
          reason: reason.trim(),
          steps: steps.map((s) => ({
            key: s.key,
            title: s.title,
            goal: s.goal,
            acceptance_criteria: s.acceptance_criteria,
            expected_files: s.expected_files,
            depends_on: parseKeys(dependsText(s)),
            grouping_reason: s.grouping_reason,
            discovery_note: s.discovery_note,
            shared_criteria: s.shared_criteria,
          })),
          manual_verification: checklist?.manual_verification ?? [],
        }),
      );
    }, "Could not save the revision");

  const decide = (decision: "approve" | "reject") =>
    act(async () => {
      setView(
        await decideChecklist(projectId, planId, task.key, decision, {
          by: name.trim(),
          reason: reason.trim(),
        }),
      );
      setReason("");
    }, "Could not record the decision");

  const runNext = (restart = false) =>
    act(
      () => runNextStep(projectId, planId, task.key, { acknowledge, restart }),
      "Could not start the step",
    );

  const resume = (stepKey: string) =>
    act(
      () => resumeStep(projectId, planId, task.key, stepKey, acknowledge),
      "Could not resume the step",
    );

  const proposed = checklist?.status === "proposed";
  const approved = checklist?.status === "approved";
  const next = steps.find((s) => s.key === view?.next_step) ?? null;
  const inProgress = steps.some(
    (s) => s.status === "running" || s.status === "proposed",
  );
  const named = name.trim() !== "" && reason.trim() !== "";
  const depsChanged = steps.some(
    (s) => dependsText(s) !== s.depends_on.join(", "),
  );

  return (
    <div data-testid={`checklist-${task.key}`}>
      <h4>Coding checklist</h4>
      <p className="sub">
        The task is split into ordered coding steps from its acceptance
        criteria. A person approves the checklist before the first coding run;
        the agent then works on one step at a time, starting from the files
        accepted for earlier steps.
      </p>
      {!checklist && (
        <div className="btn-row">
          <button
            type="button"
            disabled={disabled || busy}
            onClick={() => void propose()}
          >
            Propose checklist
          </button>
        </div>
      )}
      {checklist && (
        <div aria-label={`Checklist of ${task.key}`}>
          <p>
            <strong>v{checklist.version}</strong>{" "}
            <span className="badge">{checklist.status}</span> · proposed by{" "}
            {checklist.proposed_by}
            {checklist.proposal_reason ? ` (${checklist.proposal_reason})` : ""}
            {checklist.decided_by
              ? ` · ${checklist.status} by ${checklist.decided_by}`
              : ""}
            {checklist.decision_reason ? `: ${checklist.decision_reason}` : ""}
          </p>
          {checklist.read_only && (
            <p className="sub">This checklist version is read-only.</p>
          )}
          {checklist.warnings.length > 0 && (
            <ul className="hint" aria-label="Checklist warnings">
              {checklist.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}
          <ol aria-label="Checklist steps">
            {steps.map((s) => (
              <li key={s.key} data-testid={`step-${s.key}`}>
                <strong>
                  {s.key} {s.title}
                </strong>{" "}
                <span
                  className="badge"
                  aria-label={`Criteria count of ${s.key}`}
                >
                  {s.criteria_count} criteria
                </span>
                <StepStatus step={s} />
                <p className="sub">{s.goal}</p>
                {s.grouping_reason && (
                  <p className="sub" aria-label={`Why grouped ${s.key}`}>
                    Why grouped: {s.grouping_reason}
                  </p>
                )}
                {proposed ? (
                  <label>
                    Depends on{" "}
                    <input
                      aria-label={`Depends on of ${s.key}`}
                      value={dependsText(s)}
                      disabled={busy}
                      onChange={(e) => editDepends(s.key, e.target.value)}
                    />
                  </label>
                ) : (
                  <p className="sub">
                    Depends on: {s.depends_on.join(", ") || "nothing"}
                  </p>
                )}
                {s.acceptance_criteria.length > 0 && (
                  <ul aria-label={`Criteria of ${s.key}`}>
                    {s.acceptance_criteria.map((c) => (
                      <li key={c}>{c}</li>
                    ))}
                  </ul>
                )}
                {s.shared_criteria.length > 0 && (
                  <ul aria-label={`Shared criteria of ${s.key}`}>
                    {s.shared_criteria.map((c) => (
                      <li key={c.criterion}>
                        Also in another step: {c.criterion} ({c.reason})
                      </li>
                    ))}
                  </ul>
                )}
                <p className="sub" aria-label={`Expected files of ${s.key}`}>
                  Expected files:{" "}
                  {s.expected_files.length > 0
                    ? s.expected_files.map((f, i) => (
                        <span key={f.path}>
                          {i > 0 ? ", " : ""}
                          <code>{f.path}</code> ({f.source})
                        </span>
                      ))
                    : "discover during run"}
                  {s.expected_files.length === 0 && s.discovery_note
                    ? ` (${s.discovery_note})`
                    : ""}
                </p>
                {s.warnings.length > 0 && (
                  <ul className="hint" aria-label={`Warnings of ${s.key}`}>
                    {s.warnings.map((w) => (
                      <li key={w}>{w}</li>
                    ))}
                  </ul>
                )}
                {s.patch && (
                  <button
                    type="button"
                    onClick={() => onOpenPatch(s.patch?.id as string)}
                  >
                    Open patch of {s.key}
                  </button>
                )}
              </li>
            ))}
          </ol>
          {checklist.manual_verification.length > 0 && (
            <>
              <p className="sub">Verified manually (no coding step):</p>
              <ul aria-label="Manual verification">
                {checklist.manual_verification.map((m) => (
                  <li key={m.criterion}>
                    {m.criterion} <span className="sub">({m.reason})</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          {proposed && (
            <fieldset disabled={busy || disabled}>
              <legend>Review the checklist</legend>
              <p className="hint" role="note">
                Approving switches this task to step-based mode: task-level
                Generate patch and Run agent will be blocked, and only the
                approved coding steps can run.
              </p>
              <label>
                Your name{" "}
                <input
                  value={name}
                  maxLength={100}
                  onChange={(e) => setName(e.target.value)}
                />
              </label>{" "}
              <label>
                Reason{" "}
                <input
                  value={reason}
                  maxLength={500}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>
              <div className="btn-row">
                <button
                  type="button"
                  disabled={!named || depsChanged}
                  onClick={() => void decide("approve")}
                >
                  Approve checklist
                </button>
                <button
                  type="button"
                  className="danger"
                  disabled={!named}
                  onClick={() => void decide("reject")}
                >
                  Reject checklist
                </button>
                <button
                  type="button"
                  disabled={!named || !depsChanged}
                  onClick={() => void saveRevision()}
                >
                  Save revision
                </button>
              </div>
              <label>
                Instruction for the agent{" "}
                <input
                  value={instruction}
                  maxLength={2000}
                  onChange={(e) => setInstruction(e.target.value)}
                />
              </label>{" "}
              <button type="button" onClick={() => void agentRevise()}>
                Ask the agent to propose a revision
              </button>
              <p className="sub">
                The agent can only propose; a person approves.
              </p>
            </fieldset>
          )}
          {(checklist.status === "rejected" ||
            checklist.status === "superseded") && (
            <div className="btn-row">
              <button
                type="button"
                disabled={disabled || busy}
                onClick={() => void propose()}
              >
                Propose a new version
              </button>
            </div>
          )}
          {approved && (
            <div className="btn-row">
              {next?.resumable ? (
                <>
                  <button
                    type="button"
                    disabled={disabled || busy || inProgress}
                    onClick={() => void resume(next.key)}
                  >
                    Resume {next.key}
                  </button>
                  <button
                    type="button"
                    disabled={disabled || busy || inProgress}
                    onClick={() => void runNext(true)}
                  >
                    Restart {next.key}
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  disabled={disabled || busy || inProgress || !next}
                  onClick={() => void runNext()}
                >
                  {next ? `Run next step (${next.key})` : "All steps accepted"}
                </button>
              )}
            </div>
          )}
          {approved && inProgress && (
            <p className="sub">
              A step is running or waiting for its patch review; the next step
              starts after its patch is accepted.
            </p>
          )}
          {view && view.history.length > 1 && (
            <details>
              <summary>Checklist history</summary>
              <ul aria-label="Checklist history">
                {view.history.map((h) => (
                  <li key={h.id}>
                    v{h.version} <span className="badge">{h.status}</span> ·{" "}
                    {h.proposed_by}
                    {h.decided_by ? ` · ${h.decided_by}` : ""}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
      {error && <p role="alert">{error}</p>}
      {problems.length > 0 && (
        <ul aria-label="Checklist problems">
          {problems.map((p, i) => (
            <li key={`${p.code}:${i}`}>
              <code>{p.code}</code> {p.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
