import { useCallback, useEffect, useState } from "react";
import { DECISION_AREAS, getDecisions, saveDecision, type DecisionsOverview } from "./api";

export default function DecisionsCard({ projectId }: { projectId: string }) {
  const [data, setData] = useState<DecisionsOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [choice, setChoice] = useState("");
  const [reason, setReason] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const found = await getDecisions(projectId);
      setData({
        areas: found.areas ?? [...DECISION_AREAS],
        current: found.current ?? [],
        history: found.history ?? [],
        suggestions: found.suggestions ?? {},
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load decisions");
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  function open(area: string, current: string, suggestion: string) {
    setEditing(area);
    setChoice(current || suggestion);
    setReason("");
    setError(null);
  }

  async function submit(area: string) {
    setBusy(true);
    setError(null);
    try {
      await saveDecision(projectId, {
        area,
        choice: choice.trim(),
        reason: reason.trim(),
        name: name.trim(),
      });
      setEditing(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save the decision");
    } finally {
      setBusy(false);
    }
  }

  if (!data) return error ? <p role="alert">{error}</p> : null;
  const ready = choice.trim() !== "" && reason.trim() !== "" && name.trim() !== "";

  return (
    <div className="card" data-testid="decisions-card">
      <h2>Decisions</h2>
      <p className="sub">
        Human technology decisions are binding for code generation. Changing one keeps the old
        decision in the history.
      </p>
      {error && <p role="alert">{error}</p>}
      <ul className="stack" aria-label="Technology decisions">
        {data.areas.map((area) => {
          const current = data.current.find((d) => d.area === area);
          const suggestion = data.suggestions[area];
          return (
            <li key={area} data-testid={`decision-${area}`}>
              <div className="title-row">
                <h3>{area}</h3>
                {current ? (
                  <span>
                    {current.choice} <span className="muted">— {current.reason}</span>{" "}
                    <span className="muted">({current.decided_by})</span>
                  </span>
                ) : (
                  <span className="muted">No decision</span>
                )}
                {suggestion && (!current || current.choice !== suggestion.choice) && (
                  <span className="badge badge-ai">
                    {suggestion.label}: {suggestion.choice}
                  </span>
                )}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => open(area, current?.choice ?? "", suggestion?.choice ?? "")}
                >
                  {current ? "Change" : "Decide"}
                  <span className="visually-hidden"> {area}</span>
                </button>
              </div>
              {editing === area && (
                <div className="stack">
                  <label>
                    Choice
                    <input
                      aria-label={`Choice for ${area}`}
                      maxLength={200}
                      value={choice}
                      onChange={(e) => setChoice(e.target.value)}
                    />
                  </label>
                  <label>
                    Reason
                    <input
                      aria-label={`Reason for ${area}`}
                      maxLength={500}
                      value={reason}
                      onChange={(e) => setReason(e.target.value)}
                    />
                  </label>
                  <label>
                    Your name
                    <input
                      aria-label={`Name for ${area}`}
                      maxLength={100}
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                    />
                  </label>
                  <div className="btn-row">
                    <button
                      type="button"
                      className="primary"
                      disabled={busy || !ready}
                      onClick={() => void submit(area)}
                    >
                      Save decision
                    </button>
                    <button type="button" onClick={() => setEditing(null)}>
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {data.history.length > 0 && (
        <details>
          <summary>History ({data.history.length})</summary>
          <ul aria-label="Superseded decisions">
            {data.history.map((d) => (
              <li key={d.id} className="muted">
                {d.area}: {d.choice} — {d.reason} ({d.decided_by}, superseded)
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
