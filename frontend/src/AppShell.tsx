import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { GENERATE_STATE, requestGenerate } from "./events";
import { hrefFor, navigate, type Page } from "./routes";
import { setActiveProject, type ProjectPage } from "./activeProject";

export type BackendState = "checking" | "ok" | "unavailable" | "unexpected";

interface NavItem {
  page?: Page;
  label: string;
  /** Why a destination without a page is disabled. */
  hint?: string;
}

// Destinations without a page are shown as disabled instead of linking to a broken route.
const WORKSPACE: NavItem[] = [
  { page: "dashboard", label: "Dashboard" },
  { page: "projects", label: "Projects" },
  { page: "playground", label: "AI Playground" },
  { label: "Settings" },
];

const PROJECT: NavItem[] = [
  { page: "documents", label: "1 · Documents" },
  { page: "requirements", label: "2 · Requirements" },
  { page: "architecture", label: "3 · Architecture" },
  {
    label: "4 · Knowledge graph",
    hint: "Open the Graph tab on an architecture version",
  },
  { page: "plan", label: "5 · Plan & build" },
];

const PAGE_LABEL: Record<Page, string> = {
  dashboard: "Dashboard",
  projects: "Projects",
  documents: "Documents",
  requirements: "Requirements",
  architecture: "Architecture",
  plan: "Plan & build",
  playground: "AI Playground",
};

interface Props {
  current: Page;
  backend: BackendState;
  database?: string | null;
  environment?: string;
  version?: string;
  /** The project named in the URL, when the current page is about one. */
  projectId?: string;
  /** Project used for the Project nav links: the open one, else the last active. */
  navProjectId?: string;
  projectName?: string | null;
  projects?: { id: string; name: string }[];
  children: ReactNode;
}

const DOT: Record<BackendState, string> = {
  checking: "dot-warn",
  ok: "dot-ok",
  unavailable: "dot-bad",
  unexpected: "dot-bad",
};

const BACKEND_TEXT: Record<BackendState, string> = {
  checking: "checking",
  ok: "ok",
  unavailable: "unavailable",
  unexpected: "unexpected service",
};

function NavEntry({
  item,
  current,
  projectId,
  onNavigate,
}: {
  item: NavItem;
  current: Page;
  projectId?: string;
  onNavigate: () => void;
}) {
  if (!item.page) {
    return (
      <span className="nav-item" aria-disabled="true" title={item.hint}>
        <span className="d" aria-hidden="true" />
        <span>{item.label}</span>
        <span className="nav-tag">Soon</span>
      </span>
    );
  }
  const scoped =
    item.page === "documents" ||
    item.page === "requirements" ||
    item.page === "architecture" ||
    item.page === "plan";
  return (
    <a
      className="nav-item"
      href={hrefFor(item.page, scoped ? projectId : undefined)}
      aria-current={item.page === current ? "page" : undefined}
      onClick={onNavigate}
    >
      <span className="d" aria-hidden="true" />
      <span>{item.label}</span>
    </a>
  );
}

function ProjectSwitcher({
  current,
  projectId,
  projects,
  name,
}: {
  current: ProjectPage;
  projectId?: string;
  projects: { id: string; name: string }[];
  name: string;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const items = () =>
    Array.from(wrap.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);

  const focusAt = (i: number) => {
    const list = items();
    if (list.length) list[(i + list.length) % list.length].focus();
  };

  const close = (refocus: boolean) => {
    setOpen(false);
    if (refocus) trigger.current?.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape" && open) {
      e.preventDefault();
      close(true);
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      const list = items();
      const at = list.indexOf(document.activeElement as HTMLElement);
      if (at === -1) focusAt(e.key === "ArrowDown" ? 0 : -1);
      else focusAt(at + (e.key === "ArrowDown" ? 1 : -1));
    }
  };

  useEffect(() => {
    if (open) items()[0]?.focus();
  }, [open]);

  return (
    <div className="switcher" ref={wrap} onKeyDown={onKeyDown}>
      <button
        type="button"
        ref={trigger}
        className="switcher-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={`Switch project, current: ${name}`}
        title={name}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="switcher-name">{name}</span>
        <span className="chev" aria-hidden="true" />
      </button>
      {open && (
        <div className="switcher-menu" role="menu" id={menuId} aria-label="Projects">
          {projects.map((p) => (
            <button
              key={p.id}
              type="button"
              role="menuitem"
              className="switcher-item"
              title={p.name}
              aria-current={p.id === projectId ? "true" : undefined}
              onClick={() => {
                setActiveProject(p.id, current);
                navigate(current, p.id);
                close(false);
              }}
            >
              <span className="check" aria-hidden="true">
                {p.id === projectId ? "✓" : ""}
              </span>
              <span className="switcher-name">{p.name}</span>
            </button>
          ))}
          <div className="switcher-sep" role="separator" />
          <a
            role="menuitem"
            className="switcher-item"
            href={hrefFor("projects")}
            onClick={() => close(false)}
          >
            <span className="check" aria-hidden="true" />
            <span>All projects</span>
          </a>
        </div>
      )}
    </div>
  );
}

function Breadcrumbs({
  current,
  projectId,
  projectName,
  projects,
}: {
  current: Page;
  projectId?: string;
  projectName?: string | null;
  projects: { id: string; name: string }[];
}) {
  const inProject = !!projectId && current !== "dashboard" && current !== "playground";
  const name = projectName ?? "Project";
  const known = projects.some((p) => p.id === projectId);
  const scoped =
    current === "documents" ||
    current === "requirements" ||
    current === "architecture" ||
    current === "plan";
  return (
    <nav aria-label="Breadcrumb">
      <ol className="crumbs">
        {inProject && (
          <li>
            <a href={hrefFor("projects")}>Projects</a>
          </li>
        )}
        {inProject && scoped && known && (
          <li>
            <ProjectSwitcher
              current={current}
              projectId={projectId}
              projects={projects}
              name={name}
            />
          </li>
        )}
        {inProject && current !== "projects" && !(scoped && known) && (
          <li>
            <a href={hrefFor("projects", projectId)}>{name}</a>
          </li>
        )}
        <li>
          <span aria-current="page">
            {inProject && current === "projects" ? name : PAGE_LABEL[current]}
          </span>
        </li>
      </ol>
    </nav>
  );
}

export default function AppShell({
  current,
  backend,
  database,
  environment,
  version,
  projectId,
  navProjectId,
  projectName,
  projects = [],
  children,
}: Props) {
  const [open, setOpen] = useState(false);
  const [generating, setGenerating] = useState(false);
  const canGenerate = current === "architecture" && !!projectId;
  const close = () => setOpen(false);

  useEffect(() => {
    const onState = (e: Event) => setGenerating((e as CustomEvent<boolean>).detail === true);
    window.addEventListener(GENERATE_STATE, onState);
    return () => window.removeEventListener(GENERATE_STATE, onState);
  }, []);

  return (
    <div className="shell">
      <a
        className="skip-link"
        href="#main-content"
        onClick={(e) => {
          // A plain anchor would change the hash route, so focus the content area instead.
          e.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        Skip to content
      </a>
      <aside className="sidebar" data-open={open}>
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <span>ArchPilotAI</span>
        </div>
        <nav className="nav" aria-label="Primary">
          <p className="nav-h">Workspace</p>
          {WORKSPACE.map((item) => (
            <NavEntry key={item.label} item={item} current={current} onNavigate={close} />
          ))}
          <p className="nav-h">Project</p>
          {projectName && <p className="sidebar-project">{projectName}</p>}
          {PROJECT.map((item) => (
            <NavEntry
              key={item.label}
              item={item}
              current={current}
              projectId={navProjectId ?? projectId}
              onNavigate={close}
            />
          ))}
        </nav>
        <div className="status" role="status" aria-label="System status">
          <div>
            <span className={`dot ${DOT[backend]}`} aria-hidden="true" />
            <span>Backend: {BACKEND_TEXT[backend]}</span>
          </div>
          {database && (
            <div>
              <span className="dot dot-ok" aria-hidden="true" />
              <span>Database: {database}</span>
            </div>
          )}
          {(environment || version) && (
            <div className="status-meta">
              {[environment, version && `v${version}`].filter(Boolean).join(" · ")}
            </div>
          )}
        </div>
        <p className="sidebar-footer">Local prototype. Use synthetic data only.</p>
      </aside>

      <div className="main-column">
        <header className="topbar">
          <div className="topbar-title">
            <button
              type="button"
              className="menu-toggle"
              aria-label="Toggle navigation"
              aria-expanded={open}
              onClick={() => setOpen((o) => !o)}
            >
              ☰
            </button>
            <Breadcrumbs
              current={current}
              projectId={projectId}
              projectName={projectName}
              projects={projects}
            />
          </div>
          <div className="topbar-actions">
            <button type="button" disabled title="Export is not implemented yet">
              Export brief
            </button>
            <button
              type="button"
              className="primary"
              disabled={!canGenerate || generating}
              title={
                canGenerate ? undefined : "Open a project's Architecture page to generate a proposal"
              }
              onClick={requestGenerate}
            >
              Generate architecture
            </button>
            <span className="avatar" role="img" aria-label="Local user">
              LU
            </span>
          </div>
        </header>
        <main id="main-content" className="content" tabIndex={-1}>
          {children}
        </main>
      </div>
    </div>
  );
}
