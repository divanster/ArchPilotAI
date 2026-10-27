import { useCallback, useEffect, useState } from "react";
import {
  MAX_UPLOAD_BYTES,
  deleteDocument,
  getDocument,
  listDocuments,
  uploadDocument,
  type DocumentDetail,
  type DocumentSummary,
} from "./api";

const PREVIEW_CHARS = 5000;
const ALLOWED = /\.(txt|md)$/i;

function formatSize(bytes: number): string {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KiB`;
}

function validateFile(file: File): string | null {
  if (!ALLOWED.test(file.name)) return "Only .txt and .md files are supported";
  if (file.size === 0) return "File is empty";
  if (file.size > MAX_UPLOAD_BYTES) return "File exceeds the 1 MiB limit";
  return null;
}

export default function DocumentsSection({ projectId }: { projectId: string }) {
  const [documents, setDocuments] = useState<DocumentSummary[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [inputKey, setInputKey] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [preview, setPreview] = useState<DocumentDetail | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    setDocuments(null);
    try {
      setDocuments(await listDocuments(projectId));
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Failed to load documents");
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleUpload() {
    if (!file) {
      setUploadError("Choose a file first");
      return;
    }
    const problem = validateFile(file);
    if (problem) {
      setUploadError(problem);
      return;
    }
    setUploading(true);
    setUploadError(null);
    try {
      const created = await uploadDocument(projectId, file);
      setDocuments((current) => [created, ...(current ?? [])]);
      setFile(null);
      setInputKey((k) => k + 1);
    } catch (e) {
      setUploadError(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function handlePreview(doc: DocumentSummary) {
    setPreview(null);
    setPreviewError(null);
    setPreviewLoading(true);
    try {
      setPreview(await getDocument(projectId, doc.id));
    } catch (e) {
      setPreviewError(e instanceof Error ? e.message : "Failed to load document");
    } finally {
      setPreviewLoading(false);
    }
  }

  async function handleDelete(id: string) {
    setDeletingId(id);
    setDeleteError(null);
    try {
      await deleteDocument(projectId, id);
      setDocuments((current) => current?.filter((d) => d.id !== id) ?? null);
      setPreview((p) => (p?.id === id ? null : p));
      setConfirmingId(null);
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : "Delete failed");
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <section aria-label="Documents">
      <h4>Documents</h4>
      <div>
        <label htmlFor={`doc-file-${projectId}`}>Document file</label>
        <input
          key={inputKey}
          id={`doc-file-${projectId}`}
          type="file"
          accept=".txt,.md,text/plain,text/markdown"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setUploadError(null);
          }}
        />
        <button type="button" onClick={() => void handleUpload()} disabled={uploading}>
          {uploading ? "Uploading…" : "Upload document"}
        </button>
      </div>
      {uploadError && <p role="alert">{uploadError}</p>}

      {loadError && (
        <div role="alert">
          <p>{loadError}</p>
          <button type="button" onClick={() => void load()}>
            Retry documents
          </button>
        </div>
      )}
      {!loadError && documents === null && <p>Loading documents…</p>}
      {documents && documents.length === 0 && <p>No documents yet.</p>}
      {deleteError && <p role="alert">{deleteError}</p>}
      {documents && documents.length > 0 && (
        <ul>
          {documents.map((doc) => (
            <li key={doc.id}>
              <button type="button" onClick={() => void handlePreview(doc)}>
                {doc.filename}
              </button>{" "}
              <span>
                {formatSize(doc.size_bytes)} · {new Date(doc.created_at).toLocaleString()}
              </span>{" "}
              {confirmingId === doc.id ? (
                <span role="alertdialog" aria-label="Confirm document delete">
                  Delete “{doc.filename}”?{" "}
                  <button
                    type="button"
                    onClick={() => void handleDelete(doc.id)}
                    disabled={deletingId === doc.id}
                  >
                    {deletingId === doc.id ? "Deleting…" : "Confirm delete document"}
                  </button>
                  <button type="button" onClick={() => setConfirmingId(null)}>
                    Keep document
                  </button>
                </span>
              ) : (
                <button type="button" onClick={() => setConfirmingId(doc.id)}>
                  Delete document
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {previewLoading && <p>Loading preview…</p>}
      {previewError && <p role="alert">{previewError}</p>}
      {preview && (
        <div aria-label="Document preview" role="group">
          <h5>{preview.filename}</h5>
          {/* Rendered as a plain text node: never interpreted as HTML or Markdown. */}
          <pre>{preview.text.slice(0, PREVIEW_CHARS)}</pre>
          {preview.text.length > PREVIEW_CHARS && <p>Preview truncated.</p>}
        </div>
      )}
    </section>
  );
}
