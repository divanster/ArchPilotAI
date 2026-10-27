import { useCallback, useEffect, useState } from "react";
import { StatusBadge } from "./ui";
import {
  REQUIREMENT_STATUSES,
  extractRequirements,
  listDocuments,
  listRequirements,
  updateRequirement,
  type DocumentSummary,
  type Requirement,
  type RequirementPatch,
  type RequirementStatus,
} from "./api";

interface Props {
  projectId: string;
  /** Changes whenever the project's documents change, so the document list reloads. */
  documentsVersion?: number;
}

const TYPE_LABELS: Record<Requirement["type"], string> = {
  business: "Business",
  functional: "Functional",
  non_functional: "Non-functional",
  constraint: "Constraint",
};

function RequirementItem({
  requirement,
  documentName,
  onSave,
}: {
  requirement: Requirement;
  documentName: string;
  onSave: (patch: RequirementPatch) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(requirement.title);
  const [description, setDescription] = useState(requirement.description);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(patch: RequirementPatch, afterwards?: () => void) {
    setBusy(true);
    setError(null);
    try {
      await onSave(patch);
      afterwards?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Update failed");
    } finally {
      setBusy(false);
    }
  }

  function saveEdit() {
    if (!title.trim() || !description.trim()) {
      setError("Title and description are required");
      return;
    }
    const patch: RequirementPatch = {};
    if (title.trim() !== requirement.title) patch.title = title.trim();
    if (description.trim() !== requirement.description)
      patch.description = description.trim();
    if (Object.keys(patch).length === 0) {
      setEditing(false);
      return;
    }
    void run(patch, () => setEditing(false));
  }

  const proposed = requirement.status === "proposed";

  return (
    <li
      aria-label={`Requirement ${requirement.title}`}
      className="req-item"
      data-status={requirement.status}
    >
      <div className="row">
        <StatusBadge
          status={requirement.type}
          kind="type"
          label={TYPE_LABELS[requirement.type]}
        />
        <span className="muted">Review status:</span>
        <StatusBadge status={requirement.status} testId="requirement-status" />
      </div>
      {editing ? (
        <div className="stack" style={{ marginTop: "var(--space-3)" }}>
          <label>
            Requirement title
            <input
              value={title}
              maxLength={200}
              onChange={(e) => setTitle(e.target.value)}
            />
          </label>
          <label>
            Requirement description
            <textarea
              value={description}
              maxLength={5000}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <div className="row">
            <button
              type="button"
              className="primary"
              disabled={busy}
              onClick={saveEdit}
            >
              Save requirement
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setEditing(false);
                setTitle(requirement.title);
                setDescription(requirement.description);
                setError(null);
              }}
            >
              Cancel edit
            </button>
          </div>
        </div>
      ) : (
        <>
          <h3 style={{ marginTop: "var(--space-3)" }}>{requirement.title}</h3>
          <p>{requirement.description}</p>
        </>
      )}
      <p className="muted">Source document: {documentName}</p>
      {/* Plain text node: the quote is document content and is never rendered as markup. */}
      <blockquote aria-label="Source quote">
        {requirement.source_quote}
      </blockquote>
      <p className="hint">
        Characters {requirement.source_start}–{requirement.source_end}. A
        matching quote shows where the text came from, not that the requirement
        is correct.
      </p>
      {error && <p role="alert">{error}</p>}
      {!editing && (
        <div className="row">
          <button
            type="button"
            className="primary"
            disabled={busy || !proposed}
            onClick={() => void run({ status: "confirmed" })}
          >
            Confirm
          </button>
          <button
            type="button"
            className="danger"
            disabled={busy || !proposed}
            onClick={() => void run({ status: "rejected" })}
          >
            Reject
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => setEditing(true)}
          >
            Edit
          </button>
        </div>
      )}
    </li>
  );
}

export default function RequirementsSection({
  projectId,
  documentsVersion = 0,
}: Props) {
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [requirements, setRequirements] = useState<Requirement[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [documentId, setDocumentId] = useState("");
  const [extracting, setExtracting] = useState(false);
  const [extractError, setExtractError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [filter, setFilter] = useState<RequirementStatus | "all">("all");

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const [docs, reqs] = await Promise.all([
        listDocuments(projectId),
        listRequirements(projectId),
      ]);
      setDocuments(docs);
      setRequirements(reqs);
    } catch (e) {
      setLoadError(
        e instanceof Error ? e.message : "Failed to load requirements",
      );
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load, documentsVersion]);

  async function handleExtract() {
    if (!documentId) {
      setExtractError("Choose a document first");
      return;
    }
    setExtracting(true);
    setExtractError(null);
    setNotice(null);
    try {
      const result = await extractRequirements(projectId, documentId);
      setRequirements((current) => [
        ...(current ?? []),
        ...result.requirements,
      ]);
      setNotice(
        `Extracted ${result.requirements.length} proposed requirement(s)` +
          (result.skipped_duplicates > 0
            ? `; ${result.skipped_duplicates} already existed and were left unchanged.`
            : "."),
      );
    } catch (e) {
      setExtractError(e instanceof Error ? e.message : "Extraction failed");
    } finally {
      setExtracting(false);
    }
  }

  async function handleSave(id: string, patch: RequirementPatch) {
    const updated = await updateRequirement(projectId, id, patch);
    setRequirements(
      (current) => current?.map((r) => (r.id === id ? updated : r)) ?? null,
    );
  }

  const names = new Map(documents.map((d) => [d.id, d.filename]));
  const visible = (requirements ?? []).filter(
    (r) => filter === "all" || r.status === filter,
  );

  return (
    <section aria-label="Requirements" className="stack">
      <div className="card">
        <h2>Extract requirements</h2>
        <p className="muted">
          AI-extracted requirements are proposals only. Each one must be
          reviewed by a person before it counts.
        </p>
        <div className="field-inline">
          <div className="field">
            <label htmlFor={`extract-doc-${projectId}`}>
              Document to extract from
            </label>
            <select
              id={`extract-doc-${projectId}`}
              value={documentId}
              onChange={(e) => {
                setDocumentId(e.target.value);
                setExtractError(null);
              }}
            >
              <option value="">Select a document…</option>
              {documents.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.filename}
                </option>
              ))}
            </select>
          </div>
          <button
            type="button"
            className="primary"
            onClick={() => void handleExtract()}
            disabled={extracting}
          >
            {extracting ? "Extracting…" : "Extract requirements"}
          </button>
        </div>
        {extractError && <p role="alert">{extractError}</p>}
        {notice && (
          <p role="status" className="notice">
            {notice}
          </p>
        )}
      </div>

      <div className="card">
        <div className="page-header" style={{ marginBottom: "var(--space-3)" }}>
          <h2>Requirements</h2>
          <div className="field" style={{ marginBottom: 0 }}>
            <label htmlFor={`req-filter-${projectId}`}>Filter by status</label>
            <select
              id={`req-filter-${projectId}`}
              value={filter}
              onChange={(e) =>
                setFilter(e.target.value as RequirementStatus | "all")
              }
            >
              <option value="all">All</option>
              {REQUIREMENT_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
        </div>

        {loadError && (
          <div role="alert">
            <p>{loadError}</p>
            <button type="button" onClick={() => void load()}>
              Retry requirements
            </button>
          </div>
        )}
        {!loadError && requirements === null && (
          <p className="muted">Loading requirements…</p>
        )}
        {requirements && visible.length === 0 && (
          <p className="state">No requirements to show.</p>
        )}
        {visible.length > 0 && (
          <ul className="stack" aria-label="Requirement list">
            {visible.map((r) => (
              <RequirementItem
                key={r.id}
                requirement={r}
                documentName={names.get(r.document_id) ?? "Unknown document"}
                onSave={(patch) => handleSave(r.id, patch)}
              />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
