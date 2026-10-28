import { useEffect, useState } from "react";
import {
  getGraph,
  getIndexStatus,
  listDocuments,
  listPlans,
  type PlanSummary,
  type ProposalSummary,
} from "./api";

interface Props {
  projectId: string;
  name?: string | null;
  description?: string | null;
  proposals: ProposalSummary[];
  confirmedCount: number;
}

interface Step {
  label: string;
  done: boolean;
  detail: string;
}

/** Project title and a five-step workflow derived from stored data, never from local flags. */
export default function ProjectHeader({
  projectId,
  name,
  description,
  proposals,
  confirmedCount,
}: Props) {
  const [indexed, setIndexed] = useState<number | null>(null);
  const [graphBuilt, setGraphBuilt] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [plans, setPlans] = useState<PlanSummary[]>([]);

  const approved = proposals
    .filter((p) => p.status === "approved")
    .sort((a, b) => b.version - a.version)[0];
  const approvedId = approved?.id;

  useEffect(() => {
    let active = true;
    listDocuments(projectId)
      .then(async (docs) => {
        if (!Array.isArray(docs)) return 0;
        const states = await Promise.all(
          docs.map((d) =>
            getIndexStatus(projectId, d.id)
              .then((s) => s?.state === "indexed" || s?.state === "stale")
              .catch(() => false),
          ),
        );
        return states.filter(Boolean).length;
      })
      .then((n) => active && setIndexed(n))
      .catch(() => active && setIndexed(0));
    return () => {
      active = false;
    };
  }, [projectId]);

  useEffect(() => {
    if (!approvedId) return;
    let active = true;
    getGraph(projectId, approvedId)
      .then((g) => active && setGraphBuilt(g?.built === true))
      .catch(() => active && setGraphBuilt(false));
    return () => {
      active = false;
    };
  }, [projectId, approvedId]);

  useEffect(() => {
    let active = true;
    Promise.resolve(listPlans(projectId))
      .then((p) => active && setPlans(Array.isArray(p) ? p : []))
      .catch(() => active && setPlans([]));
    return () => {
      active = false;
    };
  }, [projectId]);

  const approvedPlan = plans
    .filter((p) => p.status === "approved")
    .sort((a, b) => b.version - a.version)[0];
  const latestPlan = [...plans].sort((a, b) => b.version - a.version)[0];

  const steps: Step[] = [
    {
      label: "Documents indexed",
      done: (indexed ?? 0) > 0,
      detail: indexed === null ? "checking" : `${indexed} indexed`,
    },
    {
      label: "Requirements confirmed",
      done: confirmedCount > 0,
      detail: `${confirmedCount} confirmed`,
    },
    {
      label: "Architecture approved",
      done: !!approved,
      detail: approved ? `V${approved.version} approved` : "not approved",
    },
    {
      label: "Knowledge graph built",
      done: !!approved && graphBuilt,
      detail: graphBuilt && approved ? "built" : "not built",
    },
    {
      label: "Plan",
      done: !!approvedPlan,
      detail: approvedPlan
        ? `V${approvedPlan.version} approved`
        : latestPlan
          ? `V${latestPlan.version} ${latestPlan.status}`
          : "not built",
    },
  ];
  const currentIndex = steps.findIndex((s) => !s.done);

  return (
    <div className="ph">
      <h1>{name ?? "Project"}</h1>
      {description && (
        <div className="ph-desc">
          <p className={expanded ? undefined : "desc-clamp"}>{description}</p>
          {(description.length > 120 || description.includes("\n")) && (
            <button
              type="button"
              className="desc-toggle"
              aria-expanded={expanded}
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? "Show less" : "Show more"}
            </button>
          )}
        </div>
      )}
      <ol className="steps" aria-label="Project workflow">
        {steps.map((s, i) => {
          const state = s.done ? "done" : i === currentIndex ? "cur" : "locked";
          return (
            <li
              key={s.label}
              className={`step ${state}`}
              aria-current={state === "cur" ? "step" : undefined}
            >
              <span className="dot-step" aria-hidden="true">
                {s.done ? "✓" : i + 1}
              </span>
              <span className="step-label">
                <b>{s.label}</b>
                <small>{s.detail}</small>
                <span className="visually-hidden">
                  {" "}
                  ({state === "done" ? "done" : state === "cur" ? "current" : "locked"})
                </span>
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
