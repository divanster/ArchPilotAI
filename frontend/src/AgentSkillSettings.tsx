import { useEffect, useRef, useState } from "react";
import {
  getAgentSkill,
  listAgentSkills,
  setAgentSkillEnabled,
  type AgentSkill,
  type AgentSkillDetail,
  type AgentSkillList,
} from "./api";

const SOURCE_LABELS: Record<string, string> = {
  "built-in": "built-in",
  vendored: "vendored",
  project: "project",
};

export default function AgentSkillSettings({
  projectId,
  disabled,
}: {
  projectId: string;
  disabled: boolean;
}) {
  const [list, setList] = useState<AgentSkillList | null>(null);
  const [open, setOpen] = useState<AgentSkillDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    setOpen(null);
    listAgentSkills(projectId)
      .then((l) => {
        if (mounted.current && l && Array.isArray(l.skills)) setList(l);
      })
      .catch(() => undefined);
  }, [projectId]);

  const toggle = async (skill: AgentSkill) => {
    setError(null);
    try {
      const next = await setAgentSkillEnabled(
        projectId,
        skill.name,
        !skill.enabled,
      );
      if (mounted.current && next && Array.isArray(next.skills)) setList(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not change the skill");
    }
  };

  const show = async (skill: AgentSkill) => {
    setError(null);
    if (open?.name === skill.name) {
      setOpen(null);
      return;
    }
    try {
      const detail = await getAgentSkill(projectId, skill.name);
      if (mounted.current && detail && typeof detail.body === "string")
        setOpen(detail);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not read the skill");
    }
  };

  if (!list || list.skills.length === 0) return null;
  return (
    <div aria-label="Agent skills">
      <h5>Skills</h5>
      <p className="sub">
        Skills are advice text the agent can load. They cannot change hooks,
        tiers, approvals, budgets or the command allowlist.
      </p>
      {error && <p role="alert">{error}</p>}
      <ul>
        {list.skills.map((s) => (
          <li key={s.name}>
            <label>
              <input
                type="checkbox"
                checked={s.enabled}
                disabled={disabled}
                onChange={() => void toggle(s)}
              />{" "}
              <code>{s.name}</code>
            </label>{" "}
            <span className="badge">{SOURCE_LABELS[s.source] ?? s.source}</span>
            {s.overrides ? (
              <span className="badge">overrides {s.overrides}</span>
            ) : null}
            <span className="sub">
              {" "}
              {s.license ? `license ${s.license} · ` : ""}
              {s.version ? `version ${s.version} · ` : ""}
              sha256{" "}
              <code title={s.content_hash}>{s.content_hash.slice(0, 12)}</code>
            </span>{" "}
            <button
              type="button"
              aria-expanded={open?.name === s.name}
              onClick={() => void show(s)}
            >
              {open?.name === s.name ? "Hide" : "View"}
            </button>
            <div className="sub">{s.description}</div>
            {open?.name === s.name && (
              <textarea
                readOnly
                aria-label={`Body of skill ${s.name}`}
                value={open.body}
                rows={12}
                style={{ width: "100%" }}
              />
            )}
          </li>
        ))}
      </ul>
      {list.problems.length > 0 && (
        <ul className="sub" aria-label="Skills that were not loaded">
          {list.problems.map((p) => (
            <li key={`${p.source}:${p.name}:${p.code}`}>
              Not loaded: {p.name || "(folder)"} ({p.source}): {p.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
