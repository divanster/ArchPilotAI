import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";

describe("App", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the name and healthy backend status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ status: "ok", service: "ArchPilotAI", environment: "test", version: "0.1.0" }),
      }),
    );
    render(<App />);
    expect(screen.getByRole("heading", { name: "ArchPilotAI" })).toBeInTheDocument();
    expect(await screen.findByText("Backend: ok")).toBeInTheDocument();
  });

  it("shows unavailable when the backend fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("down")));
    render(<App />);
    expect(await screen.findByText("Backend: unavailable")).toBeInTheDocument();
  });
});
