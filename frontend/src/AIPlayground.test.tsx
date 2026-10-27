import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AIPlayground from "./AIPlayground";

function json(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

const okBody = {
  answer: "A modular monolith keeps deployment simple.",
  provider: "fake",
  model: "fake-model",
  latency_ms: 12,
  usage: { input_tokens: 5, output_tokens: null, total_tokens: null },
};

describe("AIPlayground", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  function submit(text: string) {
    fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: text } });
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
  }

  it("renders the prompt form", () => {
    render(<AIPlayground />);
    expect(screen.getByRole("heading", { name: "AI Playground" })).toBeInTheDocument();
    expect(screen.getByLabelText("Prompt")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Generate" })).toBeEnabled();
  });

  it("shows the answer with provider metadata and unreported tokens", async () => {
    fetchMock.mockResolvedValue(json(okBody));
    render(<AIPlayground />);
    submit("Explain a modular monolith.");
    expect(await screen.findByText(okBody.answer)).toBeInTheDocument();
    expect(screen.getByText("fake")).toBeInTheDocument();
    expect(screen.getByText("fake-model")).toBeInTheDocument();
    expect(screen.getByText("12 ms")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
    expect(screen.getAllByText("not reported")).toHaveLength(2);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ prompt: "Explain a modular monolith." });
  });

  it("shows a loading state and blocks repeat clicks", async () => {
    let resolve: (r: Response) => void = () => {};
    fetchMock.mockReturnValue(new Promise<Response>((r) => (resolve = r)));
    render(<AIPlayground />);
    submit("hello");
    const button = await screen.findByRole("button", { name: "Generating…" });
    expect(button).toBeDisabled();
    resolve(json(okBody));
    expect(await screen.findByText(okBody.answer)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("shows the server error message", async () => {
    fetchMock.mockResolvedValue(json({ detail: "The AI provider timed out." }, 504));
    render(<AIPlayground />);
    submit("hello");
    expect(await screen.findByRole("alert")).toHaveTextContent("The AI provider timed out.");
    expect(screen.queryByLabelText("AI result")).not.toBeInTheDocument();
  });

  it("rejects an empty prompt without calling the API", () => {
    render(<AIPlayground />);
    submit("   ");
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a prompt.");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
