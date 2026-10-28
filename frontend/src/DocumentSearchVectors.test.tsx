import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DocumentSearch from "./DocumentSearch";
import type {
  DocumentSummary,
  EmbeddingIdentity,
  EmbeddingStatus,
  RetrievalCapabilities,
  SearchResponse,
} from "./api";

const PID = "11111111-1111-1111-1111-111111111111";
const doc: DocumentSummary = {
  id: "a-id",
  project_id: PID,
  filename: "alpha.txt",
  media_type: "text/plain",
  size_bytes: 10,
  content_sha256: "a".repeat(64),
  created_at: "2026-01-01T00:00:00Z",
};

const identity: EmbeddingIdentity = {
  provider: "fake",
  model: "fake-hash-embedding",
  dimension: 64,
  embedder_version: "fake-hash-v1",
  is_test_provider: true,
};

const caps = (over: Partial<RetrievalCapabilities> = {}) => ({
  modes: ["keyword", "vector", "hybrid"],
  vector_available: true,
  vector_unavailable_reason: null,
  embedding: identity,
  ...over,
});

const embStatus = (state: EmbeddingStatus["state"]): EmbeddingStatus => ({
  document_id: "a-id",
  state,
  chunk_count: 3,
  embedded_count: state === "embedded" ? 3 : 0,
  stale_count: 0,
  embedding: identity,
});

const searchBody = (over: Partial<SearchResponse> = {}): SearchResponse => ({
  query_terms: ["zephyr"],
  results: [
    {
      chunk_id: "c1",
      document_id: "a-id",
      filename: "alpha.txt",
      chunk_index: 0,
      text: "zephyr <b>bold</b>",
      source_start: 0,
      source_end: 18,
      score: 0.03,
      matched_terms: [],
      bm25_rank: 1,
      vector_rank: 2,
    },
  ],
  scanned_chunks: 3,
  unindexed_documents: 0,
  scan_limited: false,
  requested_mode: "hybrid",
  effective_mode: "hybrid",
  warnings: [],
  embedding: identity,
  ...over,
});

const json = (body: unknown, status = 200) =>
  ({ ok: status < 400, status, json: async () => body }) as Response;

describe("DocumentSearch vector modes", () => {
  const fetchMock = vi.fn();
  let capsReply: () => Response;
  let embState: EmbeddingStatus["state"];
  let searchReply: () => Response;

  beforeEach(() => {
    capsReply = () => json(caps());
    embState = "not_embedded";
    searchReply = () => json(searchBody());
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/retrieval/capabilities")) return capsReply();
      if (url.endsWith("/search")) return searchReply();
      if (url.endsWith("/embed")) {
        embState = "embedded";
        return json({ ...embStatus("embedded"), action: "embedded" });
      }
      if (url.endsWith("/embedding-status")) return json(embStatus(embState));
      void init;
      return json({
        document_id: "a-id",
        state: "indexed",
        chunk_count: 3,
        chunker_version: "v",
        current_chunker_version: "v",
        indexed_at: null,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  const renderIt = () =>
    render(
      <DocumentSearch
        projectId={PID}
        documents={[doc]}
        onOpenPreview={vi.fn()}
      />,
    );

  it("offers only implemented modes and labels fake vectors as test data", async () => {
    renderIt();
    const select = await screen.findByLabelText("Search mode");
    expect(
      Array.from((select as HTMLSelectElement).options).map((o) => o.value),
    ).toEqual(["keyword", "vector", "hybrid"]);
    expect(screen.getByTestId("embedding-identity")).toHaveTextContent(
      "fake / fake-hash-embedding",
    );
    expect(screen.getByTestId("test-vector-label")).toHaveTextContent(
      /not a semantic model/,
    );
  });

  it("falls back to keyword-only when capabilities fail", async () => {
    capsReply = () => json({ detail: "x" }, 500);
    renderIt();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByLabelText("Search mode")).not.toBeInTheDocument();
    expect(screen.queryByTestId("test-vector-label")).not.toBeInTheDocument();
  });

  it("falls back to keyword-only when the provider is unavailable", async () => {
    capsReply = () =>
      json(
        caps({
          modes: ["keyword"],
          vector_available: false,
          embedding: null,
        }),
      );
    renderIt();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByLabelText("Search mode")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Embed alpha.txt" }),
    ).not.toBeInTheDocument();
  });

  it("ignores unknown modes from a malformed capabilities reply", async () => {
    capsReply = () =>
      json(caps({ modes: ["keyword", "vector", "magic"] as never }));
    renderIt();
    const select = await screen.findByLabelText("Search mode");
    expect(
      Array.from((select as HTMLSelectElement).options).map((o) => o.value),
    ).toEqual(["keyword", "vector"]);
  });

  it("shows embedding status and embeds explicitly", async () => {
    renderIt();
    expect(await screen.findByText(/Not embedded/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Embed alpha.txt" }));
    expect(
      await screen.findByText(/Embedded \u00b7 3\/3 chunks/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Re-embed alpha.txt" }),
    ).toBeInTheDocument();
  });

  it("sends the selected mode and shows ranks and the fake-vector label", async () => {
    renderIt();
    const select = await screen.findByLabelText("Search mode");
    fireEvent.change(select, { target: { value: "hybrid" } });
    fireEvent.change(screen.getByLabelText("Search query"), {
      target: { value: "zephyr" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search documents" }));
    expect(
      await screen.findByText(/keyword #1 \/ vector #2/),
    ).toBeInTheDocument();
    const call = fetchMock.mock.calls.find((c) =>
      String(c[0]).endsWith("/search"),
    );
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
      mode: "hybrid",
    });
    expect(screen.getByTestId("effective-mode")).toHaveTextContent(
      /development\/test vectors/,
    );
    // Rendered as text, never as HTML.
    expect(screen.getByText(/<b>bold<\/b>/)).toBeInTheDocument();
    expect(document.querySelector("b")).toBeNull();
  });

  it("states clearly when hybrid fell back to keyword and shows warnings", async () => {
    searchReply = () =>
      json(
        searchBody({
          effective_mode: "keyword",
          warnings: [
            "Hybrid search is unavailable; showing keyword results only.",
          ],
        }),
      );
    renderIt();
    fireEvent.change(await screen.findByLabelText("Search mode"), {
      target: { value: "hybrid" },
    });
    fireEvent.change(screen.getByLabelText("Search query"), {
      target: { value: "zephyr" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search documents" }));
    expect(await screen.findByTestId("mode-notice")).toHaveTextContent(
      /Requested Hybrid.*Keyword only/,
    );
    expect(
      screen.getByText(/showing keyword results only/),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("effective-mode")).not.toBeInTheDocument();
  });

  it("reports unavailable vector results with no hits", async () => {
    searchReply = () =>
      json(
        searchBody({
          requested_mode: "vector",
          effective_mode: "none",
          results: [],
          warnings: [
            "Vector search is unavailable: no embeddings exist for this scope.",
          ],
        }),
      );
    renderIt();
    fireEvent.change(await screen.findByLabelText("Search mode"), {
      target: { value: "vector" },
    });
    fireEvent.change(screen.getByLabelText("Search query"), {
      target: { value: "zephyr" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search documents" }));
    expect(await screen.findByTestId("mode-notice")).toHaveTextContent(
      /unavailable/,
    );
    expect(screen.getByText("No matching passages found.")).toBeInTheDocument();
  });
});
