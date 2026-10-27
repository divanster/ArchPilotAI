import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import RequirementsSection from "./RequirementsSection";
import type { DocumentSummary, Requirement } from "./api";

const PID = "11111111-1111-1111-1111-111111111111";
const DID = "22222222-2222-2222-2222-222222222222";

const doc: DocumentSummary = {
  id: DID,
  project_id: PID,
  filename: "spec.md",
  media_type: "text/markdown",
  size_bytes: 100,
  content_sha256: "a".repeat(64),
  created_at: "2026-01-01T00:00:00Z",
};

function req(over: Partial<Requirement> = {}): Requirement {
  return {
    id: "33333333-3333-3333-3333-333333333333",
    project_id: PID,
    document_id: DID,
    extraction_run_id: "44444444-4444-4444-4444-444444444444",
    type: "functional",
    title: "List projects",
    description: "The system lists projects.",
    source_quote: "The system shall list projects.",
    source_start: 10,
    source_end: 41,
    status: "proposed",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...over,
  };
}

function json(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

const run = {
  id: "r",
  provider: "fake",
  model: "m",
  prompt_version: "v",
  status: "succeeded",
  latency_ms: 1,
  input_tokens: 1,
  output_tokens: 1,
};

describe("RequirementsSection", () => {
  const fetchMock = vi.fn();

  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  function initial(requirements: Requirement[] = []) {
    fetchMock.mockResolvedValueOnce(json([doc]));
    fetchMock.mockResolvedValueOnce(json(requirements));
  }

  it("shows loading, then the empty state", async () => {
    initial();
    render(<RequirementsSection projectId={PID} />);
    expect(screen.getByText("Loading requirements…")).toBeInTheDocument();
    expect(
      await screen.findByText("No requirements to show."),
    ).toBeInTheDocument();
  });

  it("shows a load error with retry", async () => {
    fetchMock.mockRejectedValueOnce(new Error("Network down"));
    render(<RequirementsSection projectId={PID} />);
    expect(await screen.findByText("Network down")).toBeInTheDocument();
    initial([req()]);
    fireEvent.click(screen.getByRole("button", { name: "Retry requirements" }));
    expect(await screen.findByText("List projects")).toBeInTheDocument();
  });

  it("requires a document before extracting", async () => {
    initial();
    render(<RequirementsSection projectId={PID} />);
    await screen.findByText("No requirements to show.");
    fireEvent.click(
      screen.getByRole("button", { name: "Extract requirements" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Choose a document first",
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("extracts, shows a loading state, and lists proposed requirements with source", async () => {
    initial();
    render(<RequirementsSection projectId={PID} />);
    await screen.findByText("No requirements to show.");
    fireEvent.change(screen.getByLabelText("Document to extract from"), {
      target: { value: DID },
    });

    let finish: (r: Response) => void = () => {};
    fetchMock.mockReturnValueOnce(
      new Promise<Response>((resolve) => (finish = resolve)),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Extract requirements" }),
    );
    expect(screen.getByRole("button", { name: "Extracting…" })).toBeDisabled();
    expect(fetchMock.mock.calls[2][0]).toContain(
      `/documents/${DID}/extract-requirements`,
    );
    expect(fetchMock.mock.calls[2][1].method).toBe("POST");

    finish(json({ run, requirements: [req()], skipped_duplicates: 0 }, 201));
    expect(await screen.findByText("List projects")).toBeInTheDocument();
    expect(screen.getByLabelText("Source quote")).toHaveTextContent(
      "The system shall list projects.",
    );
    expect(screen.getByText("Source document: spec.md")).toBeInTheDocument();
    expect(screen.getByTestId("requirement-status")).toHaveTextContent(
      "proposed",
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Extracted 1 proposed requirement(s).",
    );
  });

  it("shows validation errors from extraction and keeps existing items", async () => {
    initial([req()]);
    render(<RequirementsSection projectId={PID} />);
    await screen.findByText("List projects");
    fireEvent.change(screen.getByLabelText("Document to extract from"), {
      target: { value: DID },
    });
    fetchMock.mockResolvedValueOnce(
      json(
        {
          detail:
            "A requirement cites a quote that does not occur in the document",
        },
        422,
      ),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Extract requirements" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "does not occur in the document",
    );
    expect(
      screen.getByRole("button", { name: "Extract requirements" }),
    ).toBeEnabled();
    expect(screen.getByText("List projects")).toBeInTheDocument();
  });

  it("reports skipped duplicates on repeat extraction", async () => {
    initial([req()]);
    render(<RequirementsSection projectId={PID} />);
    await screen.findByText("List projects");
    fireEvent.change(screen.getByLabelText("Document to extract from"), {
      target: { value: DID },
    });
    fetchMock.mockResolvedValueOnce(
      json({ run, requirements: [], skipped_duplicates: 1 }, 201),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Extract requirements" }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "1 already existed",
    );
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });

  it("confirms a proposed requirement and disables further review", async () => {
    initial([req()]);
    render(<RequirementsSection projectId={PID} />);
    await screen.findByText("List projects");
    fetchMock.mockResolvedValueOnce(json(req({ status: "confirmed" })));
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() =>
      expect(screen.getByTestId("requirement-status")).toHaveTextContent(
        "confirmed",
      ),
    );
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({
      status: "confirmed",
    });
    expect(screen.getByRole("button", { name: "Confirm" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
  });

  it("rejects a proposed requirement", async () => {
    initial([req()]);
    render(<RequirementsSection projectId={PID} />);
    await screen.findByText("List projects");
    fetchMock.mockResolvedValueOnce(json(req({ status: "rejected" })));
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() =>
      expect(screen.getByTestId("requirement-status")).toHaveTextContent(
        "rejected",
      ),
    );
  });

  it("shows review errors without changing the status", async () => {
    initial([req()]);
    render(<RequirementsSection projectId={PID} />);
    await screen.findByText("List projects");
    fetchMock.mockResolvedValueOnce(
      json({ detail: "Only a proposed requirement" }, 409),
    );
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Only a proposed requirement",
    );
    expect(screen.getByTestId("requirement-status")).toHaveTextContent(
      "proposed",
    );
  });

  it("edits title and description; a confirmed item returns to proposed", async () => {
    initial([req({ status: "confirmed" })]);
    render(<RequirementsSection projectId={PID} />);
    await screen.findByText("List projects");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Requirement title"), {
      target: { value: "Show projects" },
    });
    fetchMock.mockResolvedValueOnce(
      json(req({ title: "Show projects", status: "proposed" })),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save requirement" }));
    expect(await screen.findByText("Show projects")).toBeInTheDocument();
    expect(screen.getByTestId("requirement-status")).toHaveTextContent(
      "proposed",
    );
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({
      title: "Show projects",
    });
    expect(screen.getByLabelText("Source quote")).toHaveTextContent(
      "The system shall list projects.",
    );
  });

  it("rejects blank edits client-side", async () => {
    initial([req()]);
    render(<RequirementsSection projectId={PID} />);
    await screen.findByText("List projects");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Requirement title"), {
      target: { value: "  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save requirement" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Title and description are required",
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("filters by status", async () => {
    initial([
      req(),
      req({
        id: "5",
        title: "Fast responses",
        status: "confirmed",
        type: "non_functional",
      }),
    ]);
    render(<RequirementsSection projectId={PID} />);
    await screen.findByText("Fast responses");
    fireEvent.change(screen.getByLabelText("Filter by status"), {
      target: { value: "confirmed" },
    });
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(1);
    expect(within(items[0]).getByText("Fast responses")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Filter by status"), {
      target: { value: "rejected" },
    });
    expect(screen.getByText("No requirements to show.")).toBeInTheDocument();
  });

  it("renders a hostile quote as text, not markup", async () => {
    initial([req({ source_quote: "<img src=x onerror=alert(1)>" })]);
    const { container } = render(<RequirementsSection projectId={PID} />);
    await screen.findByText("List projects");
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByLabelText("Source quote")).toHaveTextContent(
      "<img src=x",
    );
  });
});
