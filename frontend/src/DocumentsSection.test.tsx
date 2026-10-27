import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DocumentsSection from "./DocumentsSection";
import type { DocumentSummary } from "./api";

const PID = "11111111-1111-1111-1111-111111111111";
const doc: DocumentSummary = {
  id: "22222222-2222-2222-2222-222222222222",
  project_id: PID,
  filename: "notes.md",
  media_type: "text/markdown",
  size_bytes: 2048,
  content_sha256: "a".repeat(64),
  created_at: "2026-01-01T00:00:00Z",
};

function json(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

describe("DocumentsSection", () => {
  const fetchMock = vi.fn();

  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("shows loading, then filename, size and date", async () => {
    fetchMock.mockResolvedValueOnce(json([doc]));
    render(<DocumentsSection projectId={PID} />);
    expect(screen.getByText("Loading documents…")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "notes.md" })).toBeInTheDocument();
    expect(screen.getByText(/2\.0 KiB/)).toBeInTheDocument();
  });

  it("shows a load error", async () => {
    fetchMock.mockRejectedValueOnce(new Error("Network down"));
    render(<DocumentsSection projectId={PID} />);
    expect(await screen.findByText("Network down")).toBeInTheDocument();
  });

  it("rejects unsupported file types without calling the API", async () => {
    fetchMock.mockResolvedValueOnce(json([]));
    render(<DocumentsSection projectId={PID} />);
    await screen.findByText("No documents yet.");
    const file = new File(["x"], "evil.pdf", { type: "application/pdf" });
    fireEvent.change(screen.getByLabelText("Document file"), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: "Upload document" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Only .txt and .md");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("uploads a file and lists it", async () => {
    fetchMock.mockResolvedValueOnce(json([]));
    fetchMock.mockResolvedValueOnce(json(doc, 201));
    render(<DocumentsSection projectId={PID} />);
    await screen.findByText("No documents yet.");
    const file = new File(["# Hello"], "notes.md", { type: "text/markdown" });
    fireEvent.change(screen.getByLabelText("Document file"), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: "Upload document" }));
    expect(await screen.findByRole("button", { name: "notes.md" })).toBeInTheDocument();
    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(url).toContain(`/api/projects/${PID}/documents`);
    expect(init.body).toBeInstanceOf(FormData);
  });

  it("shows server errors such as duplicates", async () => {
    fetchMock.mockResolvedValueOnce(json([]));
    fetchMock.mockResolvedValueOnce(json({ detail: "Document already exists" }, 409));
    render(<DocumentsSection projectId={PID} />);
    await screen.findByText("No documents yet.");
    const file = new File(["hi"], "a.txt", { type: "text/plain" });
    fireEvent.change(screen.getByLabelText("Document file"), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: "Upload document" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Document already exists");
  });

  it("previews content as plain text, not HTML", async () => {
    fetchMock.mockResolvedValueOnce(json([doc]));
    fetchMock.mockResolvedValueOnce(json({ ...doc, text: "<b>bold</b> <script>x</script>" }));
    const { container } = render(<DocumentsSection projectId={PID} />);
    fireEvent.click(await screen.findByRole("button", { name: "notes.md" }));
    expect(await screen.findByText("<b>bold</b> <script>x</script>")).toBeInTheDocument();
    expect(container.querySelector("b")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
  });

  it("cancels, then confirms deletion", async () => {
    fetchMock.mockResolvedValueOnce(json([doc]));
    fetchMock.mockResolvedValueOnce(json(undefined, 204));
    render(<DocumentsSection projectId={PID} />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete document" }));
    fireEvent.click(screen.getByRole("button", { name: "Keep document" }));
    expect(screen.getByRole("button", { name: "notes.md" })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Delete document" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete document" }));
    await waitFor(() => expect(screen.getByText("No documents yet.")).toBeInTheDocument());
    expect((fetchMock.mock.calls[1] as [string, RequestInit])[1].method).toBe("DELETE");
  });
});
