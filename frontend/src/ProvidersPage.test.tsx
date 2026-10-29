import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProvidersPage from "./ProvidersPage";

function json(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

const providers = {
  providers: [
    {
      kind: "chat",
      provider: "fake",
      status: "configured",
      model: "fake-model-1",
    },
    {
      kind: "embeddings",
      provider: "none",
      status: "disabled",
      detail: "Vector search is off.",
    },
  ],
};

describe("ProvidersPage", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("lists providers with their status and sends no test request on load", async () => {
    fetchMock.mockResolvedValue(json(providers));
    render(<ProvidersPage />);
    expect(await screen.findByText("Status: configured")).toBeInTheDocument();
    expect(screen.getByText("Status: disabled")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(false);
    expect(
      screen.getByRole("button", { name: "Test connection: Embeddings" }),
    ).toBeDisabled();
  });

  it("runs one test request per click", async () => {
    fetchMock.mockResolvedValueOnce(json(providers));
    render(<ProvidersPage />);
    fetchMock.mockResolvedValueOnce(
      json({
        kind: "chat",
        ok: true,
        message: "Connection works.",
        latency_ms: 3,
      }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Test connection: Chat" }),
    );
    expect(
      await screen.findByText("OK: Connection works."),
    ).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(String(fetchMock.mock.calls[1][0])).toContain(
      "/api/settings/providers/chat/test",
    );
  });
});
