import { useEffect, useState } from "react";
import { fetchHealth, type HealthResponse } from "./api";
import AIPlayground from "./AIPlayground";
import AppShell, { type BackendState } from "./AppShell";
import Dashboard from "./Dashboard";
import DocumentsSection from "./DocumentsSection";
import ProjectScopedPage from "./ProjectScopedPage";
import ProjectsPage from "./ProjectsPage";
import ArchitectureSection from "./ArchitectureSection";
import RequirementsSection from "./RequirementsSection";
import { navigate, useRoute } from "./routes";

export default function App() {
  const [state, setState] = useState<BackendState>("checking");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const route = useRoute();

  useEffect(() => {
    let active = true;
    fetchHealth()
      .then((h) => {
        if (!active) return;
        setHealth(h);
        // Another service answering on the API address is not the ArchPilotAI backend.
        setState(h.service ? "ok" : "unexpected");
      })
      .catch(() => active && setState("unavailable"));
    return () => {
      active = false;
    };
  }, []);

  return (
    <AppShell current={route.page} backend={state} database={health?.database}>
      {state === "unexpected" && (
        <p role="alert">
          The service at the configured API address is not the ArchPilotAI
          backend, so requests will fail. Start the ArchPilotAI backend on that
          address or set VITE_API_URL.
        </p>
      )}
      {health?.database_fallback && (
        <p role="alert">
          Development warning: PostgreSQL was unavailable at startup, so this
          backend is using a local SQLite database. Its data is separate from
          PostgreSQL.
        </p>
      )}
      {route.page === "dashboard" && (
        <Dashboard
          backend={state}
          database={health?.database}
          databaseFallback={health?.database_fallback}
        />
      )}
      {route.page === "projects" && (
        <ProjectsPage
          key={route.arg ?? "none"}
          initialCreate={route.arg === "new"}
          initialSelectedId={
            route.arg && route.arg !== "new" ? route.arg : null
          }
          onOpenSection={(section, id) => navigate(section, id)}
        />
      )}
      {route.page === "documents" && (
        <ProjectScopedPage
          page="documents"
          title="Documents"
          subtitle="Upload .txt or .md files and preview their text."
          projectId={route.arg}
        >
          {(id) => <DocumentsSection key={id} projectId={id} />}
        </ProjectScopedPage>
      )}
      {route.page === "requirements" && (
        <ProjectScopedPage
          page="requirements"
          title="Requirements"
          subtitle="AI-proposed requirements with their source quotes, awaiting human review."
          projectId={route.arg}
        >
          {(id) => <RequirementsSection key={id} projectId={id} />}
        </ProjectScopedPage>
      )}
      {route.page === "architecture" && (
        <ProjectScopedPage
          page="architecture"
          title="Architecture"
          subtitle="Draft architecture proposals generated from confirmed requirements, pending human review."
          projectId={route.arg}
        >
          {(id) => <ArchitectureSection key={id} projectId={id} />}
        </ProjectScopedPage>
      )}
      {route.page === "playground" && <AIPlayground />}
    </AppShell>
  );
}
