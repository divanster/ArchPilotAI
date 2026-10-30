import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DocumentSearch from "./DocumentSearch";
import type { DocumentSummary, IndexStatus, SearchResponse } from "./api";

const PID = "11111111-1111-1111-1111-111111111111";
const mk = (id: string, filename: string): DocumentSummary => ({
  id,
  project_id: PID,
  filename,
  media_type: "text/plain",
  size_bytes: 10,
  content_sha256: "a".repeat(64),
  created_at: "2026-01-01T00:00:00Z",
});
const docA = mk("a-id", "alpha.txt");
const docB = mk("b-id", "beta.txt");

const status = (
  id: string,
  state: IndexStatus["state"],
  chunks = 0,
): IndexStatus => ({
  document_id: id,
  state,
  chunk_count: chunks,
  chunker_version: state === "not_indexed" ? null : "para-v1:1000:150",
  current_chunker_version: "para-v1:1000:150",
  indexed_at: null,
});

const response = (over: Partial<SearchResponse> = {}): SearchResponse => ({
  query_terms: ["zephyr"],
  results: [],
  scanned_chunks: 1,
  unindexed_documents: 0,
  scan_limited: false,
  ...over,
});

function json(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

describe("DocumentSearch", () => {
  const fetchMock = vi.fn();
  const onOpen = vi.fn();
  let statusById: Record<string, IndexStatus>;
  let searchReply: () => Response;

  beforeEach(() => {
    statusById = {
      "a-id": status("a-id", "not_indexed"),
      "b-id": status("b-id", "indexed", 3),
    };
    searchReply = () => json(response());
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/search")) return searchReply();
      const id = /documents\/([^/]+)\/index/.exec(url)?.[1] ?? "";
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as { reindex: boolean };
        statusById[id] = status(id, "indexed", 2);
        return json({
          ...statusById[id],
          action: body.reindex ? "reindexed" : "indexed",
        });
      }
      return json(statusById[id]);
    });
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    fetchMock.mockReset();
    onOpen.mockReset();
    vi.unstubAllGlobals();
  });

  const renderIt = (docs = [docA, docB]) =>
    render(
      <DocumentSearch
        projectId={PID}
        documents={docs}
        onOpenPreview={onOpen}
      />,
    );

  // Mount effects fetch capabilities and index status; awaiting their outcome keeps the
  // resulting state updates inside the test (no act(...) warnings).
  it("labels results as keyword matches, not AI answers", async () => {
    renderIt();
    expect(screen.getByText(/not an AI answer/)).toBeInTheDocument();
    await screen.findByRole("button", { name: "Index alpha.txt" });
    await screen.findByRole("button", { name: "Reindex beta.txt" });
  });

  it("shows an empty prompt without documents", async () => {
    renderIt([]);
    expect(
      await screen.findByText(/Upload a document/),
    ).toBeInTheDocument();
  });

  it("shows unindexed and indexed status with Index / Reindex actions", async () => {
    renderIt();
    expect(
      await screen.findByRole("button", { name: "Index alpha.txt" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Reindex beta.txt" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("index-state-a-id")).toHaveTextContent(
      "Not indexed",
    );
    expect(screen.getByTestId("index-state-b-id")).toHaveTextContent(
      "Indexed · 3 chunks",
    );
  });

  it("indexes a document and updates its status", async () => {
    renderIt();
    fireEvent.click(
      await screen.findByRole("button", { name: "Index alpha.txt" }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("index-state-a-id")).toHaveTextContent(
        "Indexed · 2 chunks",
      ),
    );
  });

  it("shows an indexing error", async () => {
    renderIt();
    const button = await screen.findByRole("button", {
      name: "Index alpha.txt",
    });
    fetchMock.mockImplementationOnce(async () =>
      json({ detail: "Index conflict" }, 409),
    );
    fireEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Index conflict",
    );
  });

  it("rejects an empty query without calling the API", async () => {
    renderIt();
    await screen.findByRole("button", { name: "Index alpha.txt" });
    const calls = fetchMock.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Search documents" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Enter a search query",
    );
    expect(fetchMock.mock.calls.length).toBe(calls);
  });

  it("renders results as text with source location and opens the preview", async () => {
    searchReply = () =>
      json(
        response({
          results: [
            {
              chunk_id: "c1",
              document_id: "b-id",
              filename: "beta.txt",
              chunk_index: 0,
              text: "<img src=x onerror=alert(1)> zephyr fact",
              source_start: 12,
              source_end: 52,
              score: 1.5,
              matched_terms: ["zephyr"],
            },
          ],
          unindexed_documents: 1,
        }),
      );
    renderIt();
    await screen.findByRole("button", { name: "Reindex beta.txt" });
    fireEvent.change(screen.getByLabelText("Search query"), {
      target: { value: "zephyr" },
    });
    fireEvent.change(screen.getByLabelText("Search in"), {
      target: { value: "b-id" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search documents" }));
    expect(await screen.findByText(/characters 12–52/)).toBeInTheDocument();
    expect(screen.getByText(/onerror=alert/)).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
    expect(
      screen.getByText(/1 document\(s\) are not indexed/),
    ).toBeInTheDocument();
    const searchCall = fetchMock.mock.calls.find(([u]) =>
      String(u).endsWith("/search"),
    );
    expect(JSON.parse(String(searchCall?.[1].body))).toMatchObject({
      query: "zephyr",
      document_id: "b-id",
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Open beta.txt preview" }),
    );
    expect(onOpen).toHaveBeenCalledWith(docB);
  });

  it("shows an empty-results state", async () => {
    renderIt();
    await screen.findByRole("button", { name: "Index alpha.txt" });
    fireEvent.change(screen.getByLabelText("Search query"), {
      target: { value: "nothing" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search documents" }));
    expect(
      await screen.findByText("No matching passages found."),
    ).toBeInTheDocument();
  });

  it("shows a search error", async () => {
    searchReply = () => json({ detail: "Query has no searchable terms" }, 422);
    renderIt();
    await screen.findByRole("button", { name: "Index alpha.txt" });
    fireEvent.change(screen.getByLabelText("Search query"), {
      target: { value: "!!!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search documents" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Query has no searchable terms",
    );
  });
});
