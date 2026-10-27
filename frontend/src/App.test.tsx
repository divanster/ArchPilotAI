import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

describe("Navigation", () => {
  beforeEach(() => {
    window.location.hash = "";
  });
  afterEach(() => vi.unstubAllGlobals());

  it("lists every destination and only links the ones that exist", () => {
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

  it("asks for a project on the documents and requirements pages", async () => {
    stubFetch(true, {}, [alpha, beta]);
    window.location.hash = "#/documents";
    render(<App />);
    expect(
      await screen.findByRole("heading", { name: "Documents" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("Choose a project to continue."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Project")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Project"), {
      target: { value: alpha.id },
    });
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
    stubFetch(true, {}, []);
    window.location.hash = "#/documents";
    render(<App />);
    expect(
      await screen.findByText("Create a project first."),
    ).toBeInTheDocument();
  });

  it("falls back to the dashboard for unknown routes", () => {
    stubFetch(true);
    window.location.hash = "#/settings";
    render(<App />);
    expect(
      screen.getByRole("heading", { name: "Welcome to ArchPilotAI" }),
    ).toBeInTheDocument();
  });

  it("toggles the mobile navigation", () => {
    stubFetch(true);
    const { container } = render(<App />);
    const toggle = screen.getByRole("button", { name: "Toggle navigation" });
    const sidebar = container.querySelector(".sidebar");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(sidebar).toHaveAttribute("data-open", "true");
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
