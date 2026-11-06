import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { ADVANCED_KEY, AdvancedDetails, AdvancedProvider } from "./advanced";

beforeEach(() => {
  window.localStorage.clear();
  window.location.hash = "";
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url.endsWith("/health")) {
        return new Response(
          JSON.stringify({ status: "ok", service: "ArchPilotAI", environment: "t", version: "1" }),
          { status: 200 },
        );
      }
      return new Response("[]", { status: 200 });
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("Show advanced switch", () => {
  it("is off by default and hides the AI Playground", async () => {
    render(<App />);
    const toggle = await screen.findByRole("switch", { name: /show advanced/i });
    expect(toggle).not.toBeChecked();
    expect(screen.queryByText("AI Playground")).toBeNull();
  });

  it("shows the playground when on and stores the choice", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("switch", { name: /show advanced/i }));
    await waitFor(() => expect(screen.getAllByText("AI Playground").length).toBeGreaterThan(0));
    expect(window.localStorage.getItem(ADVANCED_KEY)).toBe("1");
  });

  it("restores the stored choice", async () => {
    window.localStorage.setItem(ADVANCED_KEY, "1");
    render(<App />);
    expect(await screen.findByRole("switch", { name: /show advanced/i })).toBeChecked();
  });
});

describe("AdvancedDetails", () => {
  it("renders a summary with a details link when off, and the content when on", () => {
    const { unmount } = render(
      <AdvancedProvider>
        <AdvancedDetails summary="3 of 4 covered">
          <p>full matrix</p>
        </AdvancedDetails>
      </AdvancedProvider>,
    );
    expect(screen.getByText(/3 of 4 covered/)).toBeInTheDocument();
    expect(screen.queryByText("full matrix")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "details" }));
    expect(screen.getByText("full matrix")).toBeInTheDocument();
    unmount();

    window.localStorage.setItem(ADVANCED_KEY, "1");
    render(
      <AdvancedProvider>
        <AdvancedDetails summary="3 of 4 covered">
          <p>full matrix</p>
        </AdvancedDetails>
      </AdvancedProvider>,
    );
    expect(screen.getByText("full matrix")).toBeInTheDocument();
    expect(screen.queryByText(/3 of 4 covered/)).toBeNull();
  });
});
