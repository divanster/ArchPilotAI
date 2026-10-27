import { EmptyState, ErrorState, LoadingState, StatusBadge } from "./ui";
import { useProjectList } from "./useProjectList";
import { hrefFor } from "./routes";
import type { BackendState } from "./AppShell";

interface Props {
  backend: BackendState;
  database?: "sqlite" | "postgresql" | null;
  databaseFallback?: boolean;
}

const API_LABEL: Record<BackendState, string> = {
  checking: "Checking…",
  ok: "Online",
  unavailable: "Unavailable",
  unexpected: "Wrong service",
};

export default function Dashboard({
  backend,
  database,
  databaseFallback,
}: Props) {
  const { projects, error, reload } = useProjectList();

  const counts = { active: 0, draft: 0, archived: 0 };
  projects?.forEach((p) => (counts[p.status] += 1));
  const recent = [...(projects ?? [])]
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    .slice(0, 5);

  return (
    <section aria-label="Dashboard">
      <div className="hero">
        <h1>Welcome to ArchPilotAI</h1>
        <p>
          Turn project documents into reviewed requirements. Upload a document,
          let the AI propose requirements with exact source quotes, then confirm
          or reject each one.
        </p>
        <a className="btn btn-light" href={hrefFor("projects", "new")}>
          Create Project
        </a>
      </div>

      <div className="stat-grid" aria-label="Project overview">
        <div className="stat">
          <div className="stat-value">{projects ? projects.length : "–"}</div>
          <div className="stat-label">Total projects</div>
        </div>
        <div className="stat">
          <div className="stat-value">{projects ? counts.active : "–"}</div>
          <div className="stat-label">Active</div>
        </div>
        <div className="stat">
          <div className="stat-value">{projects ? counts.draft : "–"}</div>
          <div className="stat-label">Draft</div>
        </div>
        <div className="stat">
          <div className="stat-value">{projects ? counts.archived : "–"}</div>
          <div className="stat-label">Archived</div>
        </div>
      </div>

      <div className="two-col">
        <div className="card">
          <h2>Recent projects</h2>
          {error && (
            <ErrorState message={error} onRetry={() => void reload()} />
          )}
          {!error && projects === null && (
            <LoadingState label="Loading projects…" />
          )}
          {projects && projects.length === 0 && (
            <EmptyState>No projects yet.</EmptyState>
          )}
          {recent.length > 0 && (
            <ul aria-label="Recent projects">
              {recent.map((p) => (
                <li key={p.id} className="list-row">
                  <a href={hrefFor("projects", p.id)}>{p.name}</a>
                  <span className="row">
                    <StatusBadge status={p.status} />
                    <span className="muted">
                      {new Date(p.updated_at).toLocaleDateString()}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="card" aria-label="System status">
          <h2>System status</h2>
          <ul>
            <li className="list-row">
              <span>API service</span>
              <span
                className={`badge ${backend === "ok" ? "badge-ok" : "badge-rejected"}`}
              >
                {API_LABEL[backend]}
              </span>
            </li>
            <li className="list-row">
              <span>Database</span>
              {database ? (
                <span
                  className={`badge ${databaseFallback ? "badge-proposed" : "badge-ok"}`}
                >
                  {database === "postgresql" ? "PostgreSQL" : "SQLite"}
                  {databaseFallback ? " (fallback)" : ""}
                </span>
              ) : (
                <span className="badge">Unknown</span>
              )}
            </li>
          </ul>
        </div>
      </div>
    </section>
  );
}
