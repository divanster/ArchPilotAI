import { useCallback, useEffect, useState } from "react";
import {
  embedDocument,
  getEmbeddingStatus,
  getIndexStatus,
  getRetrievalCapabilities,
  indexDocument,
  searchDocuments,
  type DocumentSummary,
  type EmbeddingStatus,
  type IndexStatus,
  type RetrievalCapabilities,
  type SearchMode,
  type SearchResponse,
} from "./api";

const STATE_LABEL: Record<IndexStatus["state"], string> = {
  not_indexed: "Not indexed",
  indexed: "Indexed",
  stale: "Index out of date",
};

const EMBED_LABEL: Record<EmbeddingStatus["state"], string> = {
  unavailable: "Embeddings unavailable",
  not_indexed: "Index first",
  not_embedded: "Not embedded",
  embedded: "Embedded",
  stale: "Embeddings out of date",
};

const MODE_LABEL: Record<SearchMode, string> = {
  keyword: "Keyword",
  vector: "Vector",
  hybrid: "Hybrid (keyword + vector)",
};

const KNOWN_MODES: SearchMode[] = ["keyword", "vector", "hybrid"];

// Anything missing or malformed falls back to keyword-only.
function safeModes(caps: RetrievalCapabilities | null): SearchMode[] {
  if (!caps || !caps.vector_available || !Array.isArray(caps.modes)) {
    return ["keyword"];
  }
  const modes = KNOWN_MODES.filter((m) => caps.modes.includes(m));
  return modes.includes("keyword") ? modes : ["keyword"];
}

function without(map: Record<string, string>, key: string) {
  const copy = { ...map };
  delete copy[key];
  return copy;
}

function messageOf(e: unknown, fallback: string): string {
  return e instanceof Error ? e.message : fallback;
}

export default function DocumentSearch({
  projectId,
  documents,
  onOpenPreview,
}: {
  projectId: string;
  documents: DocumentSummary[];
  onOpenPreview: (doc: DocumentSummary) => void;
}) {
  const [statuses, setStatuses] = useState<Record<string, IndexStatus>>({});
  const [statusErrors, setStatusErrors] = useState<Record<string, string>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [caps, setCaps] = useState<RetrievalCapabilities | null>(null);
  const [mode, setMode] = useState<SearchMode>("keyword");
  const [embStatuses, setEmbStatuses] = useState<
    Record<string, EmbeddingStatus>
  >({});
  const [embErrors, setEmbErrors] = useState<Record<string, string>>({});
  const [embBusyId, setEmbBusyId] = useState<string | null>(null);

  const modes = safeModes(caps);
  const vectorAvailable = modes.length > 1;

  useEffect(() => {
    let cancelled = false;
    getRetrievalCapabilities(projectId)
      .then((c) => {
        if (!cancelled) setCaps(c);
      })
      .catch(() => {
        if (!cancelled) setCaps(null);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const loadEmbeddingStatus = useCallback(
    async (id: string) => {
      try {
        const s = await getEmbeddingStatus(projectId, id);
        setEmbStatuses((m) => ({ ...m, [id]: s }));
        setEmbErrors((m) => without(m, id));
      } catch (e) {
        setEmbErrors((m) => ({
          ...m,
          [id]: messageOf(e, "Embedding status unavailable"),
        }));
      }
    },
    [projectId],
  );

  const loadStatus = useCallback(
    async (id: string) => {
      try {
        const status = await getIndexStatus(projectId, id);
        setStatuses((s) => ({ ...s, [id]: status }));
        setStatusErrors((s) => without(s, id));
      } catch (e) {
        setStatusErrors((s) => ({
          ...s,
          [id]: messageOf(e, "Status unavailable"),
        }));
      }
    },
    [projectId],
  );

  const documentKey = documents.map((d) => d.id).join(",");
  useEffect(() => {
    for (const id of documentKey ? documentKey.split(",") : []) {
      void loadStatus(id);
    }
  }, [documentKey, loadStatus]);

  useEffect(() => {
    if (!vectorAvailable) return;
    for (const id of documentKey ? documentKey.split(",") : []) {
      void loadEmbeddingStatus(id);
    }
  }, [documentKey, vectorAvailable, loadEmbeddingStatus]);

  async function handleEmbed(doc: DocumentSummary, reembed: boolean) {
    setEmbBusyId(doc.id);
    try {
      const result = await embedDocument(projectId, doc.id, reembed);
      setEmbStatuses((m) => ({ ...m, [doc.id]: result }));
      setEmbErrors((m) => without(m, doc.id));
    } catch (e) {
      setEmbErrors((m) => ({
        ...m,
        [doc.id]: messageOf(e, "Embedding failed"),
      }));
    } finally {
      setEmbBusyId(null);
    }
  }

  async function handleIndex(doc: DocumentSummary, reindex: boolean) {
    setBusyId(doc.id);
    try {
      const result = await indexDocument(projectId, doc.id, reindex);
      setStatuses((s) => ({ ...s, [doc.id]: result }));
      setStatusErrors((s) => without(s, doc.id));
      // Reindexing replaces chunks, which drops their embeddings.
      if (vectorAvailable) void loadEmbeddingStatus(doc.id);
    } catch (e) {
      setStatusErrors((s) => ({
        ...s,
        [doc.id]: messageOf(e, "Indexing failed"),
      }));
    } finally {
      setBusyId(null);
    }
  }

  async function handleSearch(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = query.trim();
    if (!trimmed) {
      setSearchError("Enter a search query");
      setResponse(null);
      return;
    }
    setSearching(true);
    setSearchError(null);
    try {
      setResponse(
        await searchDocuments(
          projectId,
          trimmed,
          filter || null,
          modes.includes(mode) ? mode : "keyword",
        ),
      );
    } catch (e) {
      setResponse(null);
      setSearchError(messageOf(e, "Search failed"));
    } finally {
      setSearching(false);
    }
  }

  const byId = new Map(documents.map((d) => [d.id, d]));

  return (
    <section aria-label="Document search" className="card stack">
      <h2>Document search</h2>
      <p className="hint">
        Keyword matching over indexed document text. Results are passages from
        your documents, not an AI answer.
      </p>
      {vectorAvailable && caps?.embedding && (
        <p className="hint" data-testid="embedding-identity">
          Embedding provider: {caps.embedding.provider} / {caps.embedding.model}{" "}
          ({caps.embedding.dimension} dimensions,{" "}
          {caps.embedding.embedder_version})
        </p>
      )}
      {vectorAvailable && caps?.embedding?.is_test_provider && (
        <p className="state" role="status" data-testid="test-vector-label">
          Development/test vectors — lexical overlap only, not a semantic model.
          Do not rely on these results.
        </p>
      )}

      {documents.length === 0 ? (
        <p className="muted">Upload a document to enable search.</p>
      ) : (
        <ul aria-label="Index status">
          {documents.map((doc) => {
            const status = statuses[doc.id];
            const error = statusErrors[doc.id];
            const busy = busyId === doc.id;
            return (
              <li key={doc.id} className="list-row">
                <div>
                  {doc.filename}{" "}
                  <span className="hint" data-testid={`index-state-${doc.id}`}>
                    {status
                      ? `${STATE_LABEL[status.state]}${
                          status.state === "not_indexed"
                            ? ""
                            : ` · ${status.chunk_count} chunks`
                        }`
                      : error
                        ? ""
                        : "Checking index…"}
                  </span>
                  {error && <div role="alert">{error}</div>}
                  {vectorAvailable && (
                    <div className="hint">
                      <span data-testid={`embed-state-${doc.id}`}>
                        {embStatuses[doc.id]
                          ? `${EMBED_LABEL[embStatuses[doc.id].state]}${
                              embStatuses[doc.id].chunk_count > 0
                                ? ` · ${embStatuses[doc.id].embedded_count}/${embStatuses[doc.id].chunk_count} chunks`
                                : ""
                            }`
                          : embErrors[doc.id]
                            ? ""
                            : "Checking embeddings…"}
                      </span>
                      {embErrors[doc.id] && (
                        <div role="alert">{embErrors[doc.id]}</div>
                      )}
                    </div>
                  )}
                </div>
                <div className="actions">
                  {vectorAvailable && (
                    <button
                      type="button"
                      disabled={
                        embBusyId === doc.id ||
                        !embStatuses[doc.id] ||
                        embStatuses[doc.id].state === "not_indexed"
                      }
                      aria-label={`${
                        embStatuses[doc.id] &&
                        embStatuses[doc.id].state !== "not_embedded" &&
                        embStatuses[doc.id].state !== "not_indexed"
                          ? "Re-embed"
                          : "Embed"
                      } ${doc.filename}`}
                      onClick={() =>
                        void handleEmbed(
                          doc,
                          !!embStatuses[doc.id] &&
                            embStatuses[doc.id].state !== "not_embedded",
                        )
                      }
                    >
                      {embBusyId === doc.id
                        ? "Embedding…"
                        : embStatuses[doc.id] &&
                            embStatuses[doc.id].state !== "not_embedded" &&
                            embStatuses[doc.id].state !== "not_indexed"
                          ? "Re-embed"
                          : "Embed"}
                    </button>
                  )}
                  <button
                    type="button"
                    disabled={busy}
                    aria-label={`${
                      status && status.state !== "not_indexed"
                        ? "Reindex"
                        : "Index"
                    } ${doc.filename}`}
                    onClick={() =>
                      void handleIndex(
                        doc,
                        !!status && status.state !== "not_indexed",
                      )
                    }
                  >
                    {busy
                      ? "Indexing…"
                      : status && status.state !== "not_indexed"
                        ? "Reindex"
                        : "Index"}
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <form onSubmit={(e) => void handleSearch(e)} className="field-inline">
        <div className="field">
          <label htmlFor={`search-q-${projectId}`}>Search query</label>
          <input
            id={`search-q-${projectId}`}
            type="search"
            value={query}
            maxLength={200}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor={`search-doc-${projectId}`}>Search in</label>
          <select
            id={`search-doc-${projectId}`}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          >
            <option value="">All documents</option>
            {documents.map((d) => (
              <option key={d.id} value={d.id}>
                {d.filename}
              </option>
            ))}
          </select>
        </div>
        {vectorAvailable && (
          <div className="field">
            <label htmlFor={`search-mode-${projectId}`}>Search mode</label>
            <select
              id={`search-mode-${projectId}`}
              value={mode}
              onChange={(e) => setMode(e.target.value as SearchMode)}
            >
              {modes.map((m) => (
                <option key={m} value={m}>
                  {MODE_LABEL[m]}
                </option>
              ))}
            </select>
          </div>
        )}
        <button type="submit" className="primary" disabled={searching}>
          {searching ? "Searching…" : "Search documents"}
        </button>
      </form>

      {searchError && <p role="alert">{searchError}</p>}

      {response && (
        <div aria-label="Search results" role="region">
          {response.requested_mode &&
            response.effective_mode &&
            response.requested_mode !== response.effective_mode && (
              <p className="state" role="status" data-testid="mode-notice">
                Requested {MODE_LABEL[response.requested_mode]} search; results
                are{" "}
                {response.effective_mode === "none"
                  ? "unavailable"
                  : `${MODE_LABEL[response.effective_mode]} only`}
                .
              </p>
            )}
          {response.effective_mode &&
            response.effective_mode !== "keyword" &&
            response.effective_mode !== "none" && (
              <p className="hint" data-testid="effective-mode">
                {MODE_LABEL[response.effective_mode]} results
                {response.embedding?.is_test_provider
                  ? " (development/test vectors)"
                  : ""}
              </p>
            )}
          {(response.warnings ?? []).map((w) => (
            <p key={w} className="state" role="status">
              {w}
            </p>
          ))}
          {response.unindexed_documents > 0 && (
            <p className="state" role="status">
              {response.unindexed_documents} document(s) are not indexed and
              were not searched.
            </p>
          )}
          {response.scan_limited && (
            <p className="state" role="status">
              Search was limited to the first indexed chunks; results may be
              incomplete.
            </p>
          )}
          {response.results.length === 0 ? (
            <p className="state">No matching passages found.</p>
          ) : (
            <ol>
              {response.results.map((r) => {
                const doc = byId.get(r.document_id);
                return (
                  <li key={r.chunk_id} className="list-row">
                    <div>
                      <strong>{r.filename}</strong>{" "}
                      <span className="hint">
                        characters {r.source_start}–{r.source_end}
                      </span>
                      {(r.bm25_rank != null || r.vector_rank != null) &&
                        response.effective_mode !== "keyword" && (
                          <span className="hint">
                            {" "}
                            ·{" "}
                            {r.bm25_rank != null
                              ? `keyword #${r.bm25_rank}`
                              : "no keyword match"}
                            {" / "}
                            {r.vector_rank != null
                              ? `vector #${r.vector_rank}`
                              : "no vector match"}
                          </span>
                        )}
                      {/* Plain text node: never interpreted as HTML or Markdown. */}
                      <pre className="excerpt">{r.text}</pre>
                    </div>
                    {doc && (
                      <button type="button" onClick={() => onOpenPreview(doc)}>
                        Open {r.filename} preview
                      </button>
                    )}
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      )}
    </section>
  );
}
