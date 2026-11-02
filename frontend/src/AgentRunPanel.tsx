import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  answerCiPush,
  approveCiLoop,
  deleteCiBranch,
  getAgentDeployments,
  getAgentRouting,
  getAgentHooks,
  getAgentRun,
  getChecksStatus,
  getCiLoopApprovalInfo,
  listAgentRuns,
  resumeStep,
  revokeCiLoop,
  setAgentHookEnabled,
  startAgentRun,
  stopAgentRun,
  type AgentHook,
  type AgentDeployments,
  type AgentRouting,
  type AgentJob,
  type AgentRun,
  type AgentStepView,
  type ChecksStatus,
  type CILive,
  type CiLoopApprovalInfo,
  type CiLoopResult,
  type CiLoopView,
  type PlanTask,
} from "./api";
import AgentSkillSettings from "./AgentSkillSettings";

const POLL_MS = 1500;

const STOP_LABELS: Record<string, string> = {
  reasoning_budget_exhausted: "reasoning budget exhausted",
  token_limit: "token limit reached",
  heavy_call_limit: "heavy-model call limit reached",
  cost_limit: "cost limit reached",
};

const ESCALATION_LABELS: Record<string, string> = {
  invalid_actions: "two invalid actions in a row",
  repeated_failure: "same failure twice",
  reasoning_budget_exhausted: "light model exhausted its reasoning budget",
};

function seconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)} s`;
}

function TestsView({ step }: { step: AgentStepView }) {
  const t = step.tests;
  if (!t) return null;
  return (
    <div aria-label={`Tests of step ${step.step_no}`}>
      <p className="sub">
        {t.command_id}: {t.status}
        {t.structured
          ? ` · ${t.new_in_changed} new in changed files · ${t.new_elsewhere} new elsewhere (counted, not fixed) · ${t.pre_existing} pre-existing · ${t.fixed} fixed`
          : ` · ${t.note ?? "exit code only"}`}
      </p>
      {t.new.length > 0 && (
        <ul>
          {t.new.map((n) => (
            <li key={n.name}>
              <code>{n.name}</code> {n.message}
            </li>
          ))}
        </ul>
      )}
      {t.flaky.length > 0 && (
        <p className="sub">Flaky (passed on re-run): {t.flaky.join(", ")}</p>
      )}
      {t.ci?.workflow_url && (
        <p className="sub">
          GitHub Actions: {t.ci.conclusion ?? t.ci.status ?? "—"} ·{" "}
          <WorkflowLink url={t.ci.workflow_url} />
        </p>
      )}
    </div>
  );
}

/** Only an https link is rendered; anything else is shown as text. */
function WorkflowLink({ url }: { url: string }) {
  if (!url.startsWith("https://")) return <code>{url}</code>;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer">
      workflow run
    </a>
  );
}

function CIJobInfo({ job }: { job: AgentJob }) {
  const ci = job.ci;
  if (!ci) return null;
  return (
    <span className="sub" aria-label={`CI of job ${job.id}`}>
      {" "}
      · CI {ci.status ?? "—"}
      {ci.conclusion ? ` (${ci.conclusion})` : ""}
      {ci.branch ? (
        <>
          {" "}
          on <code>{ci.branch}</code>
        </>
      ) : null}
      {ci.kept ? " · branch kept" : ""}
      {ci.branch_deleted ? " · branch deleted" : ""}
      {ci.approved_by ? ` · approved by ${ci.approved_by}` : ""}
      {ci.report === false ? " · no JUnit report" : ""}
      {ci.error ? ` · ${ci.error}` : ""}
      {ci.workflow_url ? (
        <>
          {" "}
          · <WorkflowLink url={ci.workflow_url} />
        </>
      ) : null}
    </span>
  );
}

function CIApproval({
  ci,
  onAnswer,
}: {
  ci: CILive;
  onAnswer: (approve: boolean, by: string, keep: boolean) => Promise<void>;
}) {
  const [by, setBy] = useState("");
  const [keep, setKeep] = useState(false);
  const [sending, setSending] = useState(false);
  const answer = async (approve: boolean) => {
    setSending(true);
    try {
      await onAnswer(approve, by.trim(), keep);
    } finally {
      setSending(false);
    }
  };
  return (
    <div role="group" aria-label="CI push approval">
      <p>
        The agent wants to run <code>{ci.command_id ?? "tests"}</code> in GitHub
        Actions. This pushes the draft to the branch{" "}
        <code>{ci.branch ?? "<prefix>ci/<run-id>"}</code> (never the base
        branch) and starts the workflow. You are asked once for this run.
      </p>
      <label>
        Your name{" "}
        <input
          value={by}
          maxLength={100}
          onChange={(e) => setBy(e.target.value)}
        />
      </label>{" "}
      <label>
        <input
          type="checkbox"
          checked={keep}
          onChange={(e) => setKeep(e.target.checked)}
        />{" "}
        Keep the CI branches after the run
      </label>
      <div className="btn-row">
        <button
          type="button"
          disabled={sending || by.trim() === ""}
          onClick={() => void answer(true)}
        >
          Approve CI push
        </button>
        <button
          type="button"
          className="danger"
          disabled={sending || by.trim() === ""}
          onClick={() => void answer(false)}
        >
          Deny
        </button>
      </div>
    </div>
  );
}

function CIStatus({
  ci,
  onAnswer,
}: {
  ci: CILive;
  onAnswer: (approve: boolean, by: string, keep: boolean) => Promise<void>;
}) {
  return (
    <div aria-label="CI status">
      <h5>GitHub Actions</h5>
      <p className="sub">
        {ci.phase.replace(/_/g, " ")} · push {ci.approval}
        {ci.approved_by ? ` by ${ci.approved_by}` : ""}
        {ci.keep_branches ? " · branches kept" : ""}
        {ci.conclusion ? ` · ${ci.conclusion}` : ""}
        {ci.workflow_url ? (
          <>
            {" "}
            · <WorkflowLink url={ci.workflow_url} />
          </>
        ) : null}
      </p>
      {ci.message && <p className="hint">{ci.message}</p>}
      {ci.approval === "pending" && <CIApproval ci={ci} onAnswer={onAnswer} />}
    </div>
  );
}

/** What the person sees before the one run-scoped approval of the CI branch. */
function CiLoopApproval({
  info,
  onApprove,
  onDeny,
}: {
  info: CiLoopApprovalInfo;
  onApprove: (by: string) => Promise<void>;
  onDeny: () => Promise<void>;
}) {
  const [by, setBy] = useState("");
  const [sending, setSending] = useState(false);
  const send = async (fn: () => Promise<void>) => {
    setSending(true);
    try {
      await fn();
    } finally {
      setSending(false);
    }
  };
  return (
    <div role="group" aria-label="CI loop approval">
      <p>
        Approve pushing this run's draft to its own CI branch and running the
        workflow after each batch of edits. The approval covers this run and
        this branch only.
      </p>
      <dl aria-label="CI loop approval details">
        <dt>Repository</dt>
        <dd>{info.repository}</dd>
        <dt>Remote</dt>
        <dd>
          <code>{info.remote}</code>
        </dd>
        <dt>CI branch</dt>
        <dd>
          <code>{info.branch}</code>
        </dd>
        <dt>Base branch</dt>
        <dd>
          <code>{info.base_branch}</code>
        </dd>
        <dt>Workflow</dt>
        <dd>
          <code>{info.workflow}</code> ({info.command})
        </dd>
        <dt>Maximum CI iterations</dt>
        <dd>{info.max_iterations}</dd>
      </dl>
      <p role="note">{info.protected_notice}</p>
      <label>
        Your name{" "}
        <input
          value={by}
          maxLength={100}
          onChange={(e) => setBy(e.target.value)}
        />
      </label>
      <div className="btn-row">
        <button
          type="button"
          disabled={sending || by.trim() === ""}
          onClick={() => void send(() => onApprove(by.trim()))}
        >
          Approve CI branch {info.branch}
        </button>
        <button
          type="button"
          className="danger"
          disabled={sending}
          onClick={() => void send(onDeny)}
        >
          Deny
        </button>
      </div>
    </div>
  );
}

function CiLoopFailures({
  label,
  results,
}: {
  label: string;
  results: CiLoopResult[];
}) {
  if (results.length === 0) return null;
  return (
    <>
      <p className="sub">{label}</p>
      <ul aria-label={label}>
        {results.map((r) => (
          <li key={r.signature || r.name}>
            <code>{r.name}</code>
            {r.file ? ` (${r.file})` : ""} {r.message}
            {r.linked_via ? (
              <>
                {" "}
                · linked to changed file <code>{r.linked_via}</code>
              </>
            ) : null}
          </li>
        ))}
      </ul>
    </>
  );
}

function CiLoopStatus({
  loop,
  running,
  info,
  onApprove,
  onRevoke,
  onDeleteBranch,
  onResume,
}: {
  loop: CiLoopView;
  running: boolean;
  info: CiLoopApprovalInfo | null;
  onApprove: (by: string) => Promise<void>;
  onRevoke: () => Promise<void>;
  onDeleteBranch: (confirm: boolean) => Promise<void>;
  onResume: (() => Promise<void>) | null;
}) {
  const [confirming, setConfirming] = useState(false);
  const c = loop.counts;
  const ended = !running;
  return (
    <div aria-label="CI repair loop">
      <h5>CI repair loop</h5>
      <p className="sub">
        Coding step {loop.coding_step ?? "—"} · CI branch{" "}
        <code>{loop.branch ?? "—"}</code>
        {loop.branch_deleted ? " (deleted)" : ""}
      </p>
      <p className="sub" aria-label="CI loop approval status">
        Approval: {loop.approval}
        {loop.approved_by ? ` by ${loop.approved_by}` : ""}
      </p>
      <p className="sub" aria-label="CI loop progress">
        Iteration {loop.iteration}/{loop.max_iterations} ·{" "}
        {loop.phase.replace(/_/g, " ")}
        {loop.commit ? (
          <>
            {" "}
            · commit <code>{loop.commit.slice(0, 12)}</code>
          </>
        ) : null}
        {loop.workflow_status ? ` · workflow ${loop.workflow_status}` : ""}
        {loop.workflow_url ? (
          <>
            {" "}
            · <WorkflowLink url={loop.workflow_url} />
          </>
        ) : null}
        {loop.terminal ? ` · ended: ${loop.terminal}` : ""}
      </p>
      <p className="sub" aria-label="CI loop counts">
        {c.new} new · {c.pre_existing} pre-existing · {c.fixed} fixed ·{" "}
        {c.flaky} flaky · {c.elsewhere} elsewhere
      </p>
      <p className="sub" aria-label="CI loop budgets">
        Tokens {loop.tokens_used}/{loop.token_budget} · time{" "}
        {loop.elapsed_seconds.toFixed(1)}/{loop.time_limit_seconds} s
      </p>
      {loop.message && <p className="hint">{loop.message}</p>}
      <CiLoopFailures label="Actionable failures" results={loop.actionable} />
      <CiLoopFailures
        label="New failures elsewhere (reviewer only unless linked to a changed file)"
        results={loop.elsewhere}
      />
      <CiLoopFailures label="Flaky tests" results={loop.flaky} />
      {running && loop.approval === "pending" && info && (
        <CiLoopApproval info={info} onApprove={onApprove} onDeny={onRevoke} />
      )}
      {running && loop.approval === "approved" && (
        <div className="btn-row">
          <button
            type="button"
            className="danger"
            onClick={() => void onRevoke()}
          >
            Revoke CI approval
          </button>
        </div>
      )}
      {ended && (
        <div className="btn-row">
          {onResume && (
            <button type="button" onClick={() => void onResume()}>
              Resume {loop.coding_step}
            </button>
          )}
          {loop.branch && loop.commit && !loop.branch_deleted && (
            <>
              <label>
                <input
                  type="checkbox"
                  checked={confirming}
                  onChange={(e) => setConfirming(e.target.checked)}
                />{" "}
                Delete even if a patch uses it
              </label>
              <button
                type="button"
                className="danger"
                onClick={() => void onDeleteBranch(confirming)}
              >
                Delete CI branch
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function JobList({ jobs }: { jobs: AgentJob[] }) {
  if (jobs.length === 0) return null;
  return (
    <div aria-label="Jobs">
      <h5>Jobs</h5>
      <ul>
        {jobs.map((j) => (
          <li key={j.id}>
            <code>{j.command_id}</code>{" "}
            <span className="badge">{j.variant}</span> {j.status} · exit{" "}
            {j.exit_code ?? "—"} · {seconds(j.duration_ms)} of{" "}
            {j.timeout_seconds} s limit
            {j.truncated ? " · output truncated" : ""}
            {j.flaky === true ? " · flaky" : ""}
            {j.select_test ? ` · ${j.select_test}` : ""}
            <CIJobInfo job={j} />
          </li>
        ))}
      </ul>
    </div>
  );
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
              {f.scope ? <span className="sub"> ({f.scope})</span> : null}
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

function ModelCall({ step }: { step: AgentStepView }) {
  const c = step.call;
  if (!c) return null;
  return (
    <span className="sub" aria-label={`Model call of step ${step.step_no}`}>
      {" "}
      {c.prompt_tokens ?? 0} in / {c.completion_tokens ?? 0} out
      {c.reasoning_tokens ? `, ${c.reasoning_tokens} reasoning` : ""}
      {c.finish_reason ? `, ${c.finish_reason}` : ""}
      {c.deployment
        ? ` · ${c.deployment}${c.tier ? ` (${c.tier}` : ""}${
            c.tier && c.routing_reason ? `, ${c.routing_reason}` : ""
          }${c.tier ? ")" : c.routing_reason ? ` (${c.routing_reason})` : ""}`
        : ""}
    </span>
  );
}

function RoutingSettings({ routing }: { routing: AgentRouting }) {
  const limit = (v: number | null) => (v ? String(v) : "off");
  return (
    <div className="sub" aria-label="Model routing settings">
      Model routing: {routing.enabled ? "on" : "off"}
      {routing.enabled &&
        ` · light ${routing.light ?? "run deployment"} · heavy ${
          routing.heavy ?? "run deployment"
        }`}
      <br />
      Budgets: tokens {limit(routing.token_limit)} · heavy calls{" "}
      {limit(routing.heavy_call_limit)} · cost {limit(routing.cost_limit)}
      <br />
      Light: {routing.light_actions.join(", ")}
      <br />
      Heavy: {routing.heavy_actions.join(", ")}
      {routing.problems.map((p) => (
        <div key={p} role="alert">
          {p}
        </div>
      ))}
    </div>
  );
}

function HookDecisions({ step }: { step: AgentStepView }) {
  const found = step.hooks ?? [];
  if (found.length === 0) return null;
  return (
    <ul className="sub" aria-label={`Hooks of step ${step.step_no}`}>
      {found.map((h, i) => (
        <li key={`${h.hook}:${i}`}>
          <code>{h.hook}</code> <span className="badge">{h.result}</span>
          {h.reason ? ` ${h.reason}` : ""}
        </li>
      ))}
    </ul>
  );
}

function HookSettings({
  hooks,
  disabled,
  onToggle,
}: {
  hooks: AgentHook[];
  disabled: boolean;
  onToggle: (hook: AgentHook) => void;
}) {
  if (hooks.length === 0) return null;
  return (
    <div aria-label="Agent hooks">
      <h5>Hooks</h5>
      <p className="sub">
        Active:{" "}
        {hooks
          .filter((h) => h.enabled)
          .map((h) => h.name)
          .join(", ") || "none"}
      </p>
      <ul>
        {hooks.map((h) => (
          <li key={h.name}>
            <label>
              <input
                type="checkbox"
                checked={h.enabled}
                disabled={disabled || h.required}
                onChange={() => onToggle(h)}
              />{" "}
              <code>{h.name}</code>
            </label>{" "}
            <span className="badge">{h.point}</span>
            {h.required ? <span className="badge">locked</span> : null}
            <span className="sub"> {h.description}</span>
          </li>
        ))}
      </ul>
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
  const [hooks, setHooks] = useState<AgentHook[]>([]);
  const [deployments, setDeployments] = useState<AgentDeployments | null>(null);
  const [routing, setRouting] = useState<AgentRouting | null>(null);
  const [deployment, setDeployment] = useState("");
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

  useEffect(() => {
    getAgentDeployments()
      .then((d) => {
        if (mounted.current && d && Array.isArray(d.verified))
          setDeployments(d);
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    getAgentRouting()
      .then((r) => {
        if (mounted.current && r && Array.isArray(r.light_actions))
          setRouting(r);
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    getAgentHooks(projectId)
      .then((h) => {
        if (mounted.current && Array.isArray(h)) setHooks(h);
      })
      .catch(() => undefined);
  }, [projectId]);

  const toggleHook = async (hook: AgentHook) => {
    setError(null);
    try {
      const next = await setAgentHookEnabled(
        projectId,
        hook.name,
        !hook.enabled,
      );
      if (mounted.current && Array.isArray(next)) setHooks(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not change the hook");
    }
  };

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
      setRun(
        await startAgentRun(
          projectId,
          planId,
          task.key,
          acknowledge,
          deployment || undefined,
        ),
      );
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

  const answerCi = async (approve: boolean, by: string, keep: boolean) => {
    if (!run) return;
    setError(null);
    try {
      const ci = await answerCiPush(projectId, run.id, {
        approve,
        approved_by: by,
        keep_branches: keep,
      });
      if (mounted.current) setRun((cur) => (cur ? { ...cur, ci } : cur));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not answer the CI push");
    }
  };

  const loop = run?.ci_loop ?? null;
  const pending = running && loop?.approval === "pending";
  const [loopInfo, setLoopInfo] = useState<CiLoopApprovalInfo | null>(null);

  useEffect(() => {
    if (!pending || !runId) return;
    let cancelled = false;
    getCiLoopApprovalInfo(projectId, runId)
      .then((info) => {
        if (!cancelled && mounted.current && info && info.branch)
          setLoopInfo(info);
      })
      .catch((e) => {
        if (!cancelled && mounted.current)
          setError(
            e instanceof Error ? e.message : "Could not load the CI approval",
          );
      });
    return () => {
      cancelled = true;
    };
  }, [pending, projectId, runId]);

  const loopAction = async (
    fn: (id: string) => Promise<CiLoopView>,
    fallback: string,
  ) => {
    if (!run) return;
    setError(null);
    try {
      const next = await fn(run.id);
      if (mounted.current)
        setRun((cur) => (cur ? { ...cur, ci_loop: next } : cur));
    } catch (e) {
      setError(e instanceof Error ? e.message : fallback);
    }
  };

  const resumeLoop = async () => {
    if (!loop?.coding_step) return;
    setError(null);
    try {
      const next = await resumeStep(
        projectId,
        planId,
        task.key,
        loop.coding_step,
        acknowledge,
      );
      if (mounted.current) setRun(next);
    } catch (e) {
      const d =
        e instanceof ApiError ? (e.detail as { problems?: unknown }) : null;
      const problems =
        d && Array.isArray(d.problems) ? ` (${d.problems.join(", ")})` : "";
      setError(
        (e instanceof Error ? e.message : "Could not resume the step") +
          problems,
      );
    }
  };

  const canResume =
    !running &&
    loop !== null &&
    loop.coding_step !== null &&
    loop.commit !== null &&
    loop.terminal !== "green" &&
    !loop.branch_deleted &&
    !run?.patch_id;

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
      {routing && <RoutingSettings routing={routing} />}
      {deployments && deployments.verified.length > 0 && (
        <label>
          Model{" "}
          <select
            value={deployment}
            disabled={running}
            onChange={(e) => setDeployment(e.target.value)}
          >
            <option value="">{deployments.default} (default)</option>
            {deployments.verified
              .filter((d) => d !== deployments.default)
              .map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
          </select>
        </label>
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
      <HookSettings
        hooks={hooks}
        disabled={running}
        onToggle={(h) => void toggleHook(h)}
      />
      <AgentSkillSettings projectId={projectId} disabled={running} />
      {run && (
        <div aria-label={`Agent run of ${task.key}`}>
          <p>
            <strong>{run.status}</strong>
            {run.stop_reason && run.status !== "finished"
              ? ` (${STOP_LABELS[run.stop_reason] ?? run.stop_reason})`
              : ""}{" "}
            · step {run.steps_taken}/{run.max_steps} · tokens{" "}
            {run.input_tokens + run.output_tokens}/{run.token_budget} ·{" "}
            {seconds(run.duration_ms)}
          </p>
          {run.usage && run.usage.calls > 0 && (
            <p className="sub" aria-label="Model usage of the run">
              Model calls: {run.usage.calls}
              {run.deployment ? ` (${run.deployment})` : ""} · prompt{" "}
              {run.usage.prompt_tokens} · completion{" "}
              {run.usage.completion_tokens} (reasoning{" "}
              {run.usage.reasoning_tokens}) · {seconds(run.usage.latency_ms)}
            </p>
          )}
          {run.usage?.by_deployment && run.usage.by_deployment.length > 0 && (
            <ul className="sub" aria-label="Usage per deployment">
              {run.usage.by_deployment.map((d) => (
                <li key={d.deployment}>
                  {d.deployment}: {d.calls} calls ({d.heavy_calls} heavy) ·
                  prompt {d.prompt_tokens} · completion {d.completion_tokens}
                  {d.cost ? ` · cost ${d.cost.toFixed(4)}` : ""}
                </li>
              ))}
            </ul>
          )}
          {run.usage?.escalations && run.usage.escalations.length > 0 && (
            <ul className="sub" aria-label="Escalations of the run">
              {run.usage.escalations.map((e) => (
                <li key={e.step_no}>
                  Escalated to {e.deployment ?? "heavy model"} at step{" "}
                  {e.step_no}: {ESCALATION_LABELS[e.reason] ?? e.reason}
                </li>
              ))}
            </ul>
          )}
          {run.hooks && run.hooks.length > 0 && (
            <p className="sub" aria-label="Active hooks of the run">
              Active hooks:{" "}
              {run.hooks
                .filter((h) => h.enabled)
                .map((h) => h.name)
                .join(", ") || "none"}
            </p>
          )}
          {run.skills && run.skills.length > 0 && (
            <ul className="sub" aria-label="Skills loaded by the run">
              {run.skills.map((s) => (
                <li key={`${s.source}:${s.name}`}>
                  Skill loaded: <code>{s.name}</code> ({s.source}
                  {s.version ? `, version ${s.version}` : ""}, sha256{" "}
                  {s.content_hash.slice(0, 12)})
                </li>
              ))}
            </ul>
          )}
          {running && run.ci && <CIStatus ci={run.ci} onAnswer={answerCi} />}
          {loop && (
            <CiLoopStatus
              loop={loop}
              running={running}
              info={pending ? loopInfo : null}
              onApprove={(by) =>
                loopAction(
                  (id) => approveCiLoop(projectId, id, by),
                  "Could not approve the CI branch",
                )
              }
              onRevoke={() =>
                loopAction(
                  (id) => revokeCiLoop(projectId, id),
                  "Could not revoke the CI approval",
                )
              }
              onDeleteBranch={(confirm) =>
                loopAction(
                  (id) => deleteCiBranch(projectId, id, confirm),
                  "Could not delete the CI branch",
                )
              }
              onResume={canResume ? resumeLoop : null}
            />
          )}
          {run.steps.length > 0 && (
            <ol aria-label="Agent steps">
              {run.steps.map((s) => (
                <li
                  key={s.step_no}
                  className={s.status === "error" ? "hint" : undefined}
                >
                  <code>{s.action}</code>
                  {s.tier && (
                    <span className="badge" title="Tool tier">
                      {s.tier}
                    </span>
                  )}
                  {s.path ? ` ${s.path}` : ""} — {s.result_summary}
                  {s.status === "error" && (
                    <span className="badge">{s.error_code}</span>
                  )}
                  {s.finding_total !== null &&
                    s.finding_total !== undefined && <FindingList step={s} />}
                  <TestsView step={s} />
                  <ModelCall step={s} />
                  <HookDecisions step={s} />
                </li>
              ))}
            </ol>
          )}
          <JobList jobs={run.jobs ?? []} />
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
