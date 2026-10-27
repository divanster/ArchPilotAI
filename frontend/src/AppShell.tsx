import { useState, type ReactNode } from "react";
import { hrefFor, type Page } from "./routes";

export type BackendState = "checking" | "ok" | "unavailable" | "unexpected";

interface NavItem {
  page?: Page;
  label: string;
  icon: string;
}

// Settings has no page yet, so it is shown as disabled instead of linking to a broken route.
const NAV: NavItem[] = [
  { page: "dashboard", label: "Dashboard", icon: "▦" },
  { page: "projects", label: "Projects", icon: "▣" },
  { page: "documents", label: "Documents", icon: "☰" },
  { page: "requirements", label: "Requirements", icon: "✓" },
  { page: "architecture", label: "Architecture", icon: "◇" },
  { page: "playground", label: "AI Playground", icon: "✦" },
  { label: "Settings", icon: "⚙" },
];

interface Props {
  current: Page;
  backend: BackendState;
  database?: string | null;
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

export default function AppShell({
  current,
  backend,
  database,
  children,
}: Props) {
  const [open, setOpen] = useState(false);
  const currentLabel = NAV.find((n) => n.page === current)?.label ?? "";

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
          <span className="brand-mark" aria-hidden="true">
            A
          </span>
          <span>ArchPilotAI</span>
        </div>
        <nav className="nav" aria-label="Primary">
          {NAV.map((item) =>
            item.page ? (
              <a
                key={item.label}
                className="nav-item"
                href={hrefFor(item.page)}
                aria-current={item.page === current ? "page" : undefined}
                onClick={() => setOpen(false)}
              >
                <span>
                  <span aria-hidden="true">{item.icon}</span> {item.label}
                </span>
              </a>
            ) : (
              <span key={item.label} className="nav-item" aria-disabled="true">
                <span>
                  <span aria-hidden="true">{item.icon}</span> {item.label}
                </span>
                <span className="nav-tag">Soon</span>
              </span>
            ),
          )}
        </nav>
        <p className="sidebar-footer">
          Local prototype. Use synthetic data only.
        </p>
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
            <span>{currentLabel}</span>
          </div>
          <div className="status-pills">
            <span className="pill" role="status">
              <span className={`dot ${DOT[backend]}`} aria-hidden="true" />
              <span>Backend: {BACKEND_TEXT[backend]}</span>
            </span>
            {database && (
              <span className="pill">
                <span className="dot dot-ok" aria-hidden="true" />
                <span>Database: {database}</span>
              </span>
            )}
          </div>
        </header>
        <main id="main-content" className="content" tabIndex={-1}>
          {children}
        </main>
      </div>
    </div>
  );
}
