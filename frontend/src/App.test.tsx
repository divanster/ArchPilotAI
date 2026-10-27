import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const health = { status: "ok", service: "ArchPilotAI", environment: "test", version: "0.1.0" };

function stubFetch(healthOk: boolean, extra: Record<string, unknown> = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return healthOk
          ? Promise.resolve({ ok: true, status: 200, json: async () => ({ ...health, ...extra }) })
          : Promise.reject(new Error("down"));
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => [] });
    }),
  );
}

describe("App", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the name and healthy backend status", async () => {
    stubFetch(true);
    render(<App />);
    expect(screen.getByRole("heading", { name: "ArchPilotAI" })).toBeInTheDocument();
    expect(await screen.findByText("Backend: ok")).toBeInTheDocument();
    expect(await screen.findByText("No projects yet.")).toBeInTheDocument();
  });

  it("shows unavailable when the backend fails", async () => {
    stubFetch(false);
    render(<App />);
    expect(await screen.findByText("Backend: unavailable")).toBeInTheDocument();
  });

  it("shows a development warning only when SQLite fallback is active", async () => {
    stubFetch(true, { database: "sqlite", database_fallback: true });
    render(<App />);
    expect(await screen.findByText("Database: sqlite")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Development warning");
  });

  it("shows no warning for PostgreSQL", async () => {
    stubFetch(true, { database: "postgresql", database_fallback: false });
    render(<App />);
    expect(await screen.findByText("Database: postgresql")).toBeInTheDocument();
    expect(screen.queryByText(/Development warning/)).toBeNull();
  });
});
