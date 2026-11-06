import { useEffect, useState } from "react";
import {
  getAgentRun,
  getChecklist,
  getGitSettings,
  getPlan,
  listAgentRuns,
  listChanges,
  listPatches,
  listPlans,
  listProposals,
  listRequirements,
} from "./api";
import { hrefFor, navigate, type Page } from "./routes";

export interface NextStepState {
  requirements: { total: number; confirmed: number };
  architecture: { drafts: number; approved: number };
  plan: { drafts: number; approved: number };
  ciApprovalPending: boolean;
  proposedPatch: boolean;
  changeToReview: boolean;
  /** The first approved-plan task without an accepted patch, with its checklist state. */
  openTask: { key: string; checklist: "none" | "proposed" | "approved" } | null;
  acceptedPatches: number;
  repositoryConnected: boolean;
}

export interface NextStep {
  id: string;
  title: string;
  detail: string;
  action: string;
  page: Page;
}

export function deriveNextStep(s: NextStepState): NextStep {
  if (s.requirements.confirmed === 0) {
    return s.requirements.total === 0
      ? {
          id: "confirm-requirements",
          title: "Confirm requirements",
          detail: "No requirements yet. Upload a document and extract them first.",
          action: "Open documents",
          page: "documents",
        }
      : {
          id: "confirm-requirements",
          title: "Confirm requirements",
          detail: `${s.requirements.total} requirement(s) wait for your confirmation.`,
          action: "Review requirements",
          page: "requirements",
        };
  }
  if (s.architecture.approved === 0) {
    return {
      id: "approve-architecture",
      title: "Approve architecture",
      detail:
        s.architecture.drafts > 0
          ? "A draft architecture is ready for your review."
          : "Generate an architecture from the confirmed requirements.",
      action: s.architecture.drafts > 0 ? "Review architecture" : "Generate architecture",
      page: "architecture",
    };
  }
  if (s.plan.approved === 0) {
    return {
      id: "approve-plan",
      title: "Approve plan",
      detail: s.plan.drafts > 0 ? "A draft plan is ready for your review." : "Build the implementation plan.",
      action: s.plan.drafts > 0 ? "Review plan" : "Build plan",
      page: "plan",
    };
  }
  if (s.ciApprovalPending) {
    return {
      id: "approve-ci-branch",
      title: "Approve CI branch",
      detail: "An agent run waits for your approval to push its CI branch.",
      action: "Approve CI branch",
      page: "plan",
    };
  }
  if (s.proposedPatch) {
    return {
      id: "review-patch",
      title: "Review patch",
      detail: "A proposed patch waits for your decision.",
      action: "Review patch",
      page: "plan",
    };
  }
  if (s.changeToReview) {
    return {
      id: "review-change",
      title: "Review change request",
      detail: "A change request waits for review.",
      action: "Review change request",
      page: "changes",
    };
  }
  if (s.openTask) {
    const key = s.openTask.key;
    if (s.openTask.checklist === "none") {
      return {
        id: "propose-checklist",
        title: "Propose checklist",
        detail: `Propose a checklist for ${key}.`,
        action: "Propose checklist",
        page: "plan",
      };
    }
    if (s.openTask.checklist === "proposed") {
      return {
        id: "approve-checklist",
        title: "Approve checklist",
        detail: `The checklist for ${key} waits for approval.`,
        action: "Approve checklist",
        page: "plan",
      };
    }
    return {
      id: "run-next-step",
      title: "Run next step",
      detail: `Run the next step of ${key}.`,
      action: "Run next step",
      page: "plan",
    };
  }
  if (s.acceptedPatches > 0) {
    return s.repositoryConnected
      ? {
          id: "push-pr",
          title: "Push and open PR",
          detail: "Accepted patches can be pushed to a branch and opened as a pull request.",
          action: "Push and open PR",
          page: "plan",
        }
      : {
          id: "push-pr",
          title: "Push and open PR",
          detail: "Connect a repository first, then push and open a pull request.",
          action: "Connect repository",
          page: "repository",
        };
  }
  return {
    id: "done",
    title: "All caught up",
    detail: "Nothing needs your attention right now.",
    action: "Open plan",
    page: "plan",
  };
}

async function loadState(projectId: string): Promise<NextStepState> {
  const [reqs, props, plans, changes, git] = await Promise.all([
    listRequirements(projectId),
    listProposals(projectId),
    listPlans(projectId),
    listChanges(projectId).catch(() => []),
    getGitSettings(projectId).catch(() => null),
  ]);
  const state: NextStepState = {
    requirements: {
      total: reqs.length,
      confirmed: reqs.filter((r) => r.status === "confirmed").length,
    },
    architecture: {
      drafts: props.filter((p) => p.status === "draft").length,
      approved: props.filter((p) => p.status === "approved").length,
    },
    plan: {
      drafts: plans.filter((p) => p.status === "draft").length,
      approved: plans.filter((p) => p.status === "approved").length,
    },
    ciApprovalPending: false,
    proposedPatch: false,
    changeToReview: changes.some((c) => c.status === "draft" || c.status === "analysed"),
    openTask: null,
    acceptedPatches: 0,
    repositoryConnected: !!git,
  };
  const approved = [...plans]
    .filter((p) => p.status === "approved")
    .sort((a, b) => b.version - a.version)[0];
  if (!approved || state.architecture.approved === 0) return state;
  const plan = await getPlan(projectId, approved.id);
  for (const task of plan.tasks.slice(0, 30)) {
    const patches = await listPatches(projectId, plan.id, task.key);
    if (patches.some((p) => p.status === "proposed")) state.proposedPatch = true;
    const accepted = patches.some((p) => p.status === "accepted");
    if (accepted) state.acceptedPatches += 1;
    const runs = await listAgentRuns(projectId, plan.id, task.key).catch(() => []);
    for (const run of runs.filter((r) => r.status === "running")) {
      const full = await getAgentRun(projectId, run.id).catch(() => null);
      if (full?.ci_loop?.approval === "pending" || full?.ci?.approval === "pending") {
        state.ciApprovalPending = true;
      }
    }
    if (!accepted && !state.openTask) {
      const view = await getChecklist(projectId, plan.id, task.key).catch(() => null);
      const cl = view?.checklist;
      state.openTask = {
        key: task.key,
        checklist: !cl || cl.status === "rejected" || cl.status === "superseded"
          ? "none"
          : cl.status === "proposed"
            ? "proposed"
            : "approved",
      };
    }
  }
  return state;
}

/** One card with the one thing to do next, derived from the real project state. */
export default function NextStepCard({ projectId }: { projectId: string }) {
  const [step, setStep] = useState<NextStep | null>(null);

  useEffect(() => {
    let live = true;
    setStep(null);
    loadState(projectId)
      .then((s) => {
        if (live) setStep(deriveNextStep(s));
      })
      .catch(() => {
        if (live) setStep(null);
      });
    return () => {
      live = false;
    };
  }, [projectId]);

  if (!step) return null;
  return (
    <section className="card next-step" aria-label="Next step" data-step={step.id}>
      <p className="eyebrow">Next step</p>
      <h2>{step.title}</h2>
      <p className="sub">{step.detail}</p>
      <a
        className="primary button-link"
        href={hrefFor(step.page, projectId)}
        onClick={(e) => {
          e.preventDefault();
          navigate(step.page, projectId);
        }}
      >
        {step.action}
      </a>
    </section>
  );
}
