import { useCallback, useEffect, useState } from "react";
import {
  getIndexStatus,
  indexDocument,
  searchDocuments,
  type DocumentSummary,
  type IndexStatus,
  type SearchResponse,
} from "./api";

const STATE_LABEL: Record<IndexStatus["state"], string> = {
  not_indexed: "Not indexed",
  indexed: "Indexed",
  stale: "Index out of date",
};

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

  async function handleIndex(doc: DocumentSummary, reindex: boolean) {
    setBusyId(doc.id);
    try {
      const result = await indexDocument(projectId, doc.id, reindex);
      setStatuses((s) => ({ ...s, [doc.id]: result }));
      setStatusErrors((s) => without(s, doc.id));
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
      setResponse(await searchDocuments(projectId, trimmed, filter || null));
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
                </div>
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
        <button type="submit" className="primary" disabled={searching}>
          {searching ? "Searching…" : "Search documents"}
        </button>
      </form>

      {searchError && <p role="alert">{searchError}</p>}

      {response && (
        <div aria-label="Search results" role="region">
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
