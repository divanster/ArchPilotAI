import { useEffect, useState } from "react";
import {
  fetchHealth,
  listProjects,
  type HealthResponse,
  type Project,
} from "./api";
import AIPlayground from "./AIPlayground";
import AppShell, { type BackendState } from "./AppShell";
import Dashboard from "./Dashboard";
import DocumentsSection from "./DocumentsSection";
import ProjectScopedPage from "./ProjectScopedPage";
import ProjectsPage from "./ProjectsPage";
import ProvidersPage from "./ProvidersPage";
import ArchitectureSection from "./ArchitectureSection";
import PlanSection from "./PlanSection";
import RepositoryPage from "./RepositoryPage";
import RequirementsSection from "./RequirementsSection";
import {
  PROJECT_PAGES,
  getActiveProject,
  setActiveProject,
} from "./activeProject";
import { navigate, useRoute } from "./routes";

/** Project list for the shell's name and switcher; only fetched once a project is in play. */
function useProjects(enabled: boolean) {
  const [projects, setProjects] = useState<Project[]>([]);
  useEffect(() => {
    if (!enabled) return;
    let active = true;
    listProjects()
      .then((all) => active && setProjects(Array.isArray(all) ? all : []))
      .catch(() => active && setProjects([]));
    return () => {
      active = false;
    };
  }, [enabled]);
  return projects;
}

export default function App() {
  const [state, setState] = useState<BackendState>("checking");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [activeId, setActiveId] = useState<string | null>(getActiveProject);
  const route = useRoute();
  const scoped = (PROJECT_PAGES as string[]).includes(route.page);
  const routeId = route.arg && route.arg !== "new" ? route.arg : undefined;

  // A deep link such as #/architecture/<id> makes that project the active one.
  useEffect(() => {
    if (scoped && routeId) {
      setActiveProject(routeId, route.page as (typeof PROJECT_PAGES)[number]);
      setActiveId(routeId);
    }
  }, [scoped, routeId, route.page]);

  const scopedId = scoped ? (routeId ?? activeId ?? undefined) : undefined;
  const projectId = scoped
    ? scopedId
    : route.page === "projects"
      ? routeId
      : undefined;
  const navProjectId = projectId ?? activeId ?? undefined;
  const projects = useProjects(!!navProjectId);
  const projectName = projects.find((p) => p.id === projectId)?.name ?? null;

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
    <AppShell
      current={route.page}
      backend={state}
      database={health?.database}
      environment={health?.environment}
      version={health?.version}
      projectId={projectId}
      navProjectId={navProjectId}
      projectName={projectName}
      projects={projects}
    >
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
          projectId={scopedId}
        >
          {(id) => (
            <DocumentsSection
              key={id}
              projectId={id}
              openDocumentId={route.sub}
            />
          )}
        </ProjectScopedPage>
      )}
      {route.page === "requirements" && (
        <ProjectScopedPage
          page="requirements"
          title="Requirements"
          subtitle="AI-proposed requirements with their source quotes, awaiting human review."
          projectId={scopedId}
        >
          {(id) => <RequirementsSection key={id} projectId={id} />}
        </ProjectScopedPage>
      )}
      {route.page === "architecture" && (
        <ProjectScopedPage
          page="architecture"
          title="Architecture"
          subtitle="Draft architecture proposals generated from confirmed requirements, pending human review."
          projectId={scopedId}
          projectHeader
        >
          {(id, project) => (
            <ArchitectureSection
              key={id}
              projectId={id}
              projectName={project?.name}
              projectDescription={project?.description}
            />
          )}
        </ProjectScopedPage>
      )}
      {route.page === "plan" && (
        <ProjectScopedPage
          page="plan"
          title="Plan & build"
          subtitle="Ordered implementation tasks derived from the approved architecture, pending human review."
          projectId={scopedId}
          projectHeader
        >
          {(id, project) => (
            <PlanSection
              key={id}
              projectId={id}
              projectName={project?.name}
              projectDescription={project?.description}
            />
          )}
        </ProjectScopedPage>
      )}
      {route.page === "repository" && (
        <ProjectScopedPage
          page="repository"
          title="Repository"
          subtitle="Connect an external Git repository to commit accepted patches to a branch."
          projectId={scopedId}
        >
          {(id) => <RepositoryPage key={id} projectId={id} />}
        </ProjectScopedPage>
      )}
      {route.page === "playground" && <AIPlayground />}
      {route.page === "providers" && <ProvidersPage />}
    </AppShell>
  );
}
