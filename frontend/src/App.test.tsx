import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import App from "./App";

const health = {
  status: "ok",
  service: "ArchPilotAI",
  environment: "test",
  version: "0.1.0",
};

const alpha = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "Alpha",
  description: "First",
  status: "active",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-02-01T00:00:00Z",
};
const beta = {
  ...alpha,
  id: "22222222-2222-2222-2222-222222222222",
  name: "Beta",
  status: "draft",
};

function stubFetch(
  healthOk: boolean,
  extra: Record<string, unknown> = {},
  projects: unknown[] = [],
  projectsStatus = 200,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return healthOk
          ? Promise.resolve({
              ok: true,
              status: 200,
              json: async () => ({ ...health, ...extra }),
            })
          : Promise.reject(new Error("down"));
      }
      if (url.endsWith("/api/projects")) {
        return Promise.resolve({
          ok: projectsStatus < 400,
          status: projectsStatus,
          json: async () => {
            if (projectsStatus >= 400) throw new Error("not json");
            return projects;
          },
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => [] });
    }),
  );
}

describe("App", () => {
  beforeEach(() => {
    window.location.hash = "";
  });
  afterEach(() => vi.unstubAllGlobals());

  it("shows the shell, dashboard and healthy backend status", async () => {
    stubFetch(true);
    render(<App />);
    expect(
      screen.getByRole("navigation", { name: "Primary" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Welcome to ArchPilotAI" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Backend: ok")).toBeInTheDocument();
    expect(await screen.findByText("No projects yet.")).toBeInTheDocument();
  });

  it("shows unavailable when the backend fails", async () => {
    stubFetch(false);
    render(<App />);
    expect(await screen.findByText("Backend: unavailable")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
  });

  it("flags a service that is not the ArchPilotAI backend", async () => {
    stubFetch(true, { service: "" });
    render(<App />);
    expect(
      await screen.findByText("Backend: unexpected service"),
    ).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "not the ArchPilotAI backend",
    );
  });

  it("shows a development warning only when SQLite fallback is active", async () => {
    stubFetch(true, { database: "sqlite", database_fallback: true });
    render(<App />);
    expect(await screen.findByText("Database: sqlite")).toBeInTheDocument();
    expect(screen.getByText(/Development warning/)).toBeInTheDocument();
    expect(await screen.findByText("SQLite (fallback)")).toBeInTheDocument();
  });

  it("shows no warning for PostgreSQL", async () => {
    stubFetch(true, { database: "postgresql", database_fallback: false });
    render(<App />);
    expect(await screen.findByText("Database: postgresql")).toBeInTheDocument();
    expect(screen.queryByText(/Development warning/)).toBeNull();
    expect(await screen.findByText("PostgreSQL")).toBeInTheDocument();
  });
});

// The shell fetches /health and the dashboard fetches /api/projects on mount; awaiting their
// outcome keeps those state updates inside the test (no act(...) warnings).
async function dashboardSettled() {
  expect(await screen.findByText("Backend: ok")).toBeInTheDocument();
  expect(await screen.findByText("No projects yet.")).toBeInTheDocument();
}

describe("Navigation", () => {
  beforeEach(() => {
    window.location.hash = "";
  });
  afterEach(() => vi.unstubAllGlobals());

  it("lists every destination and only links the ones that exist", async () => {
    stubFetch(true);
    render(<App />);
    const nav = screen.getByRole("navigation", { name: "Primary" });
    for (const name of [
      "Dashboard",
      "Projects",
      "Documents",
      "Requirements",
      "AI Playground",
    ]) {
      expect(
        within(nav).getByRole("link", { name: new RegExp(name) }),
      ).toHaveAttribute("href", expect.stringMatching(/^#\//));
    }
    expect(within(nav).queryByRole("link", { name: /Settings/ })).toBeNull();
    expect(
      within(nav).getByText("Settings").closest("[aria-disabled]"),
    ).toHaveAttribute("aria-disabled", "true");
    expect(
      within(nav).getByText(/Knowledge graph/).closest("[aria-disabled]"),
    ).toHaveAttribute("aria-disabled", "true");
    expect(within(nav).queryByRole("link", { name: /Knowledge graph/ })).toBeNull();
    await dashboardSettled();
  });

  it("disables Export and the top bar generate button outside a project architecture page", async () => {
    stubFetch(true);
    render(<App />);
    expect(screen.getByRole("button", { name: "Export brief" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Generate architecture" })).toBeDisabled();
    await dashboardSettled();
  });

  it("marks the current page and switches pages", async () => {
    stubFetch(true, {}, [alpha]);
    render(<App />);
    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(
      within(nav).getByRole("link", { name: /Dashboard/ }),
    ).toHaveAttribute("aria-current", "page");

    fireEvent.click(within(nav).getByRole("link", { name: /Projects/ }));
    expect(
      await screen.findByRole("heading", { name: "Projects" }),
    ).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /Projects/ })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(
      await screen.findByRole("button", { name: "Alpha" }),
    ).toBeInTheDocument();

    fireEvent.click(within(nav).getByRole("link", { name: /AI Playground/ }));
    expect(
      await screen.findByRole("heading", { name: "AI Playground" }),
    ).toBeInTheDocument();
  });

  it("shows an empty state with a Projects link and no selector", async () => {
    localStorage.clear();
    stubFetch(true, {}, [alpha, beta]);
    for (const page of ["documents", "requirements", "architecture"]) {
      window.location.hash = `#/${page}`;
      const { unmount } = render(<App />);
      expect(await screen.findByText("No project is open.")).toBeInTheDocument();
      expect(
        screen.getByRole("link", { name: /Go to Projects/ }),
      ).toHaveAttribute("href", "#/projects");
      expect(screen.queryByLabelText("Project", { exact: true })).toBeNull();
      unmount();
    }
  });

  it("deep link activates the project and shows no selector", async () => {
    localStorage.clear();
    stubFetch(true, {}, [alpha, beta]);
    window.location.hash = `#/documents/${alpha.id}`;
    render(<App />);
    expect(await screen.findByLabelText("Document file")).toBeInTheDocument();
    expect(screen.queryByLabelText("Project", { exact: true })).toBeNull();
    expect(localStorage.getItem("archpilot.activeProject")).toBe(alpha.id);
  });

  it("uses the stored active project when the URL has none", async () => {
    localStorage.clear();
    localStorage.setItem("archpilot.activeProject", alpha.id);
    stubFetch(true, {}, [alpha]);
    window.location.hash = "#/documents";
    render(<App />);
    expect(await screen.findByLabelText("Document file")).toBeInTheDocument();
  });

  it("opens the requirements page for a project from the URL", async () => {
    stubFetch(true, {}, [alpha]);
    window.location.hash = `#/requirements/${alpha.id}`;
    render(<App />);
    expect(
      await screen.findByLabelText("Document to extract from"),
    ).toBeInTheDocument();
  });

  it("explains when a project is missing or none exist", async () => {
    localStorage.clear();
    stubFetch(true, {}, []);
    window.location.hash = `#/documents/${alpha.id}`;
    render(<App />);
    expect(
      await screen.findByText("That project no longer exists."),
    ).toBeInTheDocument();
  });

  it("falls back to the dashboard for unknown routes", async () => {
    stubFetch(true);
    window.location.hash = "#/settings";
    render(<App />);
    expect(
      screen.getByRole("heading", { name: "Welcome to ArchPilotAI" }),
    ).toBeInTheDocument();
    await dashboardSettled();
  });

  it("toggles the mobile navigation", async () => {
    stubFetch(true);
    const { container } = render(<App />);
    const toggle = screen.getByRole("button", { name: "Toggle navigation" });
    const sidebar = container.querySelector(".sidebar");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(sidebar).toHaveAttribute("data-open", "true");
    await dashboardSettled();
  });

  it("opens the create-project dialog from the dashboard action", async () => {
    stubFetch(true);
    render(<App />);
    const link = screen.getByRole("link", { name: "Create Project" });
    expect(link).toHaveAttribute("href", "#/projects/new");
    fireEvent.click(link);
    expect(
      await screen.findByRole("dialog", { name: "New project" }),
    ).toBeInTheDocument();
  });
});

describe("Dashboard", () => {
  beforeEach(() => {
    window.location.hash = "";
  });
  afterEach(() => vi.unstubAllGlobals());

  it("summarises projects and lists the most recent first", async () => {
    stubFetch(true, {}, [beta, alpha]);
    render(<App />);
    const recent = await screen.findByRole("list", { name: "Recent projects" });
    const names = within(recent)
      .getAllByRole("link")
      .map((a) => a.textContent);
    // alpha was updated after beta in the fixtures' ordering rule (equal dates keep order).
    expect(names).toHaveLength(2);
    const overview = screen.getByLabelText("Project overview");
    expect(
      within(overview).getByText("Total projects").previousSibling,
    ).toHaveTextContent("2");
    expect(within(recent).getByRole("link", { name: "Alpha" })).toHaveAttribute(
      "href",
      `#/projects/${alpha.id}`,
    );
  });

  it("shows a loading state, then an error with retry for a failed project list", async () => {
    stubFetch(true, {}, [], 404);
    render(<App />);
    expect(screen.getByText("Loading projects…")).toBeInTheDocument();
    const alerts = await screen.findAllByRole("alert");
    expect(
      alerts.some((a) =>
        /no \/api\/projects endpoint \(404\)/.test(a.textContent ?? ""),
      ),
    ).toBe(true);
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

describe("Project switcher", () => {
  const longName = "A very long project name that should be truncated in the top bar";
  const gamma = { ...alpha, id: "33333333-3333-3333-3333-333333333333", name: longName };

  beforeEach(() => {
    localStorage.clear();
    window.location.hash = `#/documents/${alpha.id}`;
  });
  afterEach(() => vi.unstubAllGlobals());

  async function trigger() {
    return screen.findByRole("button", { name: /Switch project/ });
  }

  // The documents page loads its list and search capabilities on mount; awaiting the loaded
  // list keeps those state updates inside the test (no act(...) warnings).
  async function documentsSettled() {
    await waitFor(() =>
      expect(screen.getByText("No documents yet.")).toBeInTheDocument(),
    );
  }

  it("opens and closes, Escape returns focus to the trigger", async () => {
    stubFetch(true, {}, [alpha, beta]);
    render(<App />);
    const btn = await trigger();
    expect(btn).toHaveAttribute("aria-haspopup", "menu");
    expect(btn).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("menu")).toBeNull();

    fireEvent.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "true");
    const menu = screen.getByRole("menu");
    const active = within(menu).getByRole("menuitem", { name: /Alpha/ });
    expect(active).toHaveAttribute("aria-current", "true");

    fireEvent.keyDown(menu, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(btn).toHaveFocus();

    fireEvent.click(btn);
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("menu")).toBeNull();
    await documentsSettled();
  });

  it("moves between items with the arrow keys", async () => {
    stubFetch(true, {}, [alpha, beta]);
    render(<App />);
    fireEvent.click(await trigger());
    const items = within(screen.getByRole("menu")).getAllByRole("menuitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveFocus();
    fireEvent.keyDown(items[0], { key: "ArrowDown" });
    expect(items[1]).toHaveFocus();
    fireEvent.keyDown(items[1], { key: "ArrowDown" });
    expect(items[2]).toHaveFocus();
    fireEvent.keyDown(items[2], { key: "ArrowDown" });
    expect(items[0]).toHaveFocus();
    fireEvent.keyDown(items[0], { key: "ArrowUp" });
    expect(items[2]).toHaveFocus();
    await documentsSettled();
  });

  it("selecting a project switches it", async () => {
    stubFetch(true, {}, [alpha, beta]);
    render(<App />);
    fireEvent.click(await trigger());
    fireEvent.click(screen.getByRole("menuitem", { name: /Beta/ }));
    expect(window.location.hash).toBe(`#/documents/${beta.id}`);
    expect(localStorage.getItem("archpilot.activeProject")).toBe(beta.id);
    expect(screen.queryByRole("menu")).toBeNull();
    await screen.findByRole("button", { name: "Switch project, current: Beta" });
    await documentsSettled();
  });

  it("All projects links to the Projects page", async () => {
    stubFetch(true, {}, [alpha, beta]);
    render(<App />);
    fireEvent.click(await trigger());
    expect(screen.getByRole("menuitem", { name: "All projects" })).toHaveAttribute(
      "href",
      "#/projects",
    );
    await documentsSettled();
  });

  it("truncates long names and keeps the full name in title", async () => {
    stubFetch(true, {}, [alpha, gamma]);
    window.location.hash = `#/documents/${gamma.id}`;
    render(<App />);
    const btn = await screen.findByRole("button", { name: /Switch project/ });
    expect(btn).toHaveAttribute("title", longName);
    const css = readFileSync(resolve(__dirname, "styles.css"), "utf8");
    expect(css).toMatch(/\.switcher-name\s*{[^}]*text-overflow:\s*ellipsis/);
    expect(css).toMatch(/\.switcher-trigger\s*{[^}]*max-width:\s*220px/);
    expect(css).toMatch(/\.crumbs\s*{[^}]*flex-wrap:\s*nowrap/);
    await documentsSettled();
  });
});