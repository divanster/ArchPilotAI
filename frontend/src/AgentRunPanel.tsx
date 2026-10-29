import { useCallback, useEffect, useRef, useState } from "react";
import {
  getAgentRun,
  getChecksStatus,
  listAgentRuns,
  startAgentRun,
  stopAgentRun,
  type AgentRun,
  type AgentStepView,
  type ChecksStatus,
  type PlanTask,
} from "./api";

const POLL_MS = 1500;

function seconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)} s`;
}

function FindingList({ step }: { step: AgentStepView }) {
  const found = step.findings ?? [];
  return (
    <div aria-label={`Findings of step ${step.step_no}`}>
      {found.length > 0 && (
        <ul>
          {found.map((f, i) => (
            <li key={`${f.file}:${f.line}:${f.code}:${i}`}>
              <code>
                {f.file}:{f.line}
              </code>{" "}
              <span className="badge">{`${f.tool} ${f.code}`}</span> {f.message}
            </li>
          ))}
        </ul>
      )}
      {step.finding_total !== null && step.finding_total > found.length && (
        <p className="sub">
          ? {step.finding_total - found.length} more not shown
        </p>
      )}
      <p className="sub">
        {step.finding_total ?? 0} actionable · {step.findings_not_shown}{" "}
        environment
      </p>
      {step.findings_not_shown > 0 && (
        <>
          <p className="sub">
            {step.findings_not_shown} environment finding(s) hidden (missing
            packages or modules)
          </p>
          <ul className="sub">
            {Object.entries(step.environment_reasons ?? {}).map(
              ([reason, n]) => (
                <li key={reason}>
                  {n} × {reason}
                </li>
              ),
            )}
          </ul>
        </>
      )}
    </div>
  );
}

export default function AgentRunPanel({
  projectId,
  planId,
  task,
  acknowledge,
  disabled,
  onOpenPatch,
}: {
  projectId: string;
  planId: string;
  task: PlanTask;
  acknowledge: boolean;
  disabled: boolean;
  onOpenPatch: (patchId: string) => void;
}) {
  const [run, setRun] = useState<AgentRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [checks, setChecks] = useState<ChecksStatus | null>(null);
  const mounted = useRef(true);
  const runId = run?.id ?? null;
  const running = run?.status === "running";

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    getChecksStatus()
      .then((s) => {
        if (mounted.current && s && s.tools) setChecks(s);
      })
      .catch(() => undefined);
  }, []);

  // Pick up the latest run of this task (it may still be running after a reload).
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const list = await listAgentRuns(projectId, planId, task.key);
        if (cancelled || !Array.isArray(list) || list.length === 0) return;
        const latest = await getAgentRun(projectId, list[0].id);
        if (!cancelled && Array.isArray(latest.steps))
          setRun((cur) => cur ?? latest);
      } catch {
        // The panel stays empty; starting a run still works.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId, planId, task.key]);

  const poll = useCallback(async () => {
    if (!runId) return;
    try {
      const next = await getAgentRun(projectId, runId);
      if (mounted.current) setRun(next);
    } catch (e) {
      if (mounted.current)
        setError(e instanceof Error ? e.message : "Could not load the run");
    }
  }, [projectId, runId]);

  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(timer);
  }, [running, poll]);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      setRun(await startAgentRun(projectId, planId, task.key, acknowledge));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the agent");
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    if (!run) return;
    setError(null);
    try {
      setRun(await stopAgentRun(projectId, run.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not stop the run");
    }
  };

  return (
    <div data-testid={`agent-${task.key}`}>
      <h4>Agent run</h4>
      <p className="sub">
        The agent edits a private draft and runs static checks only. Nothing is
        executed or written to disk; the result is a patch for you to review.
      </p>
      {checks && (
        <p
          className="sub"
          role={checks.ok ? undefined : "alert"}
          aria-label="Static checkers"
        >
          Static checkers:{" "}
          {Object.entries(checks.tools)
            .map(
              ([name, t]) =>
                `${name} ${t.available ? (t.version ?? "available") : "UNAVAILABLE"}`,
            )
            .join(" ? ")}
          {!checks.ok &&
            " ? ruff and mypy are required; a run would stop immediately."}
        </p>
      )}
      <div className="btn-row">
        <button
          type="button"
          disabled={disabled || busy || running || checks?.ok === false}
          onClick={() => void start()}
        >
          Run agent
        </button>
        {running && (
          <button
            type="button"
            className="danger"
            disabled={run?.stop_requested}
            onClick={() => void stop()}
          >
            {run?.stop_requested ? "Stopping…" : "Stop"}
          </button>
        )}
      </div>
      {error && <p role="alert">{error}</p>}
      {run && (
        <div aria-label={`Agent run of ${task.key}`}>
          <p>
            <strong>{run.status}</strong>
            {run.stop_reason && run.status !== "finished"
              ? ` (${run.stop_reason})`
              : ""}{" "}
            · step {run.steps_taken}/{run.max_steps} · tokens{" "}
            {run.input_tokens + run.output_tokens}/{run.token_budget} ·{" "}
            {seconds(run.duration_ms)}
          </p>
          {run.steps.length > 0 && (
            <ol aria-label="Agent steps">
              {run.steps.map((s) => (
                <li
                  key={s.step_no}
                  className={s.status === "error" ? "hint" : undefined}
                >
                  <code>{s.action}</code>
                  {s.path ? ` ${s.path}` : ""} — {s.result_summary}
                  {s.status === "error" && (
                    <span className="badge">{s.error_code}</span>
                  )}
                  {s.finding_total !== null &&
                    s.finding_total !== undefined && <FindingList step={s} />}
                </li>
              ))}
            </ol>
          )}
          {run.stop_reason === "checks_unavailable" && (
            <p role="alert">
              The run stopped because a static checker (ruff or mypy) is not
              available. No patch was produced; nothing was checked.
            </p>
          )}
          {run.summary && <p>{run.summary}</p>}
          {run.patch_id && (
            <button
              type="button"
              onClick={() => onOpenPatch(run.patch_id as string)}
            >
              Open patch
            </button>
          )}
        </div>
      )}
    </div>
  );
}
