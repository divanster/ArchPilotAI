import { useEffect, useState } from "react";
import { fetchHealth, type HealthResponse } from "./api";
import AIPlayground from "./AIPlayground";
import ProjectsPage from "./ProjectsPage";

type State = "checking" | "ok" | "unavailable";

export default function App() {
  const [state, setState] = useState<State>("checking");
  const [health, setHealth] = useState<HealthResponse | null>(null);

  useEffect(() => {
    let active = true;
    fetchHealth()
      .then((h) => {
        if (!active) return;
        setHealth(h);
        setState("ok");
      })
      .catch(() => active && setState("unavailable"));
    return () => {
      active = false;
    };
  }, []);

  return (
    <main>
      <h1>ArchPilotAI</h1>
      <p role="status">Backend: {state}</p>
      {health?.database && <p>Database: {health.database}</p>}
      {health?.database_fallback && (
        <p role="alert">
          Development warning: PostgreSQL was unavailable at startup, so this backend is using a
          local SQLite database. Its data is separate from PostgreSQL.
        </p>
      )}
      <ProjectsPage />
      <AIPlayground />
    </main>
  );
}
