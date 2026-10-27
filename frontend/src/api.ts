export interface HealthResponse {
  status: "ok";
  service: string;
  environment: string;
  version: string;
  database?: "sqlite" | "postgresql" | null;
  database_fallback?: boolean;
}

const API_URL: string = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export async function fetchHealth(): Promise<HealthResponse> {
  const response = await fetch(`${API_URL}/health`);
  if (!response.ok) {
    throw new Error(`Health check failed: ${response.status}`);
  }
  return (await response.json()) as HealthResponse;
}

export type ProjectStatus = "draft" | "active" | "archived";

export const PROJECT_STATUSES: ProjectStatus[] = ["draft", "active", "archived"];

export interface Project {
  id: string;
  name: string;
  description: string | null;
  status: ProjectStatus;
  created_at: string;
  updated_at: string;
}

export interface ProjectInput {
  name: string;
  description: string | null;
  status: ProjectStatus;
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail
        .map((d) => (typeof d === "object" && d && "msg" in d ? String(d.msg) : "invalid value"))
        .join("; ");
    }
  } catch {
    // fall through to generic message
  }
  return `Request failed (${response.status})`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: typeof init?.body === "string" ? { "Content-Type": "application/json" } : undefined,
  });
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const listProjects = () => request<Project[]>("/api/projects");

export const createProject = (input: ProjectInput) =>
  request<Project>("/api/projects", { method: "POST", body: JSON.stringify(input) });

export const updateProject = (id: string, input: Partial<ProjectInput>) =>
  request<Project>(`/api/projects/${id}`, { method: "PATCH", body: JSON.stringify(input) });

export const deleteProject = (id: string) =>
  request<void>(`/api/projects/${id}`, { method: "DELETE" });

export const MAX_UPLOAD_BYTES = 1_048_576;

export interface DocumentSummary {
  id: string;
  project_id: string;
  filename: string;
  media_type: string;
  size_bytes: number;
  content_sha256: string;
  created_at: string;
}

export interface DocumentDetail extends DocumentSummary {
  text: string;
}

const documentsPath = (projectId: string) => `/api/projects/${projectId}/documents`;

export const listDocuments = (projectId: string) =>
  request<DocumentSummary[]>(documentsPath(projectId));

export const getDocument = (projectId: string, documentId: string) =>
  request<DocumentDetail>(`${documentsPath(projectId)}/${documentId}`);

export function uploadDocument(projectId: string, file: File) {
  const body = new FormData();
  body.append("file", file);
  return request<DocumentSummary>(documentsPath(projectId), { method: "POST", body });
}

export const deleteDocument = (projectId: string, documentId: string) =>
  request<void>(`${documentsPath(projectId)}/${documentId}`, { method: "DELETE" });
