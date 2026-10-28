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

export const PROJECT_STATUSES: ProjectStatus[] = [
  "draft",
  "active",
  "archived",
];

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
        .map((d) =>
          typeof d === "object" && d && "msg" in d
            ? String(d.msg)
            : "invalid value",
        )
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
    headers:
      typeof init?.body === "string"
        ? { "Content-Type": "application/json" }
        : undefined,
  });
  if (!response.ok) {
    const message = await errorMessage(response);
    // A 404 without a JSON "detail" does not come from this backend's router (FastAPI always
    // sends one), which usually means another service is listening on the API address.
    if (response.status === 404 && message.startsWith("Request failed")) {
      throw new Error(
        `The server at ${API_URL} has no ${path} endpoint (404). ` +
          "Check that the ArchPilotAI backend is running on this address.",
      );
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const listProjects = () => request<Project[]>("/api/projects");

export const createProject = (input: ProjectInput) =>
  request<Project>("/api/projects", {
    method: "POST",
    body: JSON.stringify(input),
  });

export const updateProject = (id: string, input: Partial<ProjectInput>) =>
  request<Project>(`/api/projects/${id}`, {
    method: "PATCH",
    body: JSON.stringify(input),
  });

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

const documentsPath = (projectId: string) =>
  `/api/projects/${projectId}/documents`;

export const listDocuments = (projectId: string) =>
  request<DocumentSummary[]>(documentsPath(projectId));

export const getDocument = (projectId: string, documentId: string) =>
  request<DocumentDetail>(`${documentsPath(projectId)}/${documentId}`);

export function uploadDocument(projectId: string, file: File) {
  const body = new FormData();
  body.append("file", file);
  return request<DocumentSummary>(documentsPath(projectId), {
    method: "POST",
    body,
  });
}

export const deleteDocument = (projectId: string, documentId: string) =>
  request<void>(`${documentsPath(projectId)}/${documentId}`, {
    method: "DELETE",
  });

export interface IndexStatus {
  document_id: string;
  state: "not_indexed" | "indexed" | "stale";
  chunk_count: number;
  chunker_version: string | null;
  current_chunker_version: string;
  indexed_at: string | null;
}

export interface IndexResult extends IndexStatus {
  action: "indexed" | "reindexed" | "unchanged";
}

export type SearchMode = "keyword" | "vector" | "hybrid";

export interface EmbeddingIdentity {
  provider: string;
  model: string;
  dimension: number;
  embedder_version: string;
  is_test_provider: boolean;
}

export interface SearchResult {
  chunk_id: string;
  document_id: string;
  filename: string;
  chunk_index: number;
  text: string;
  source_start: number;
  source_end: number;
  score: number;
  matched_terms: string[];
  bm25_rank?: number | null;
  vector_rank?: number | null;
}

export interface SearchResponse {
  query_terms: string[];
  results: SearchResult[];
  scanned_chunks: number;
  unindexed_documents: number;
  scan_limited: boolean;
  requested_mode?: SearchMode;
  effective_mode?: SearchMode | "none";
  warnings?: string[];
  embedding?: EmbeddingIdentity | null;
}

export interface RetrievalCapabilities {
  modes: SearchMode[];
  vector_available: boolean;
  vector_unavailable_reason: string | null;
  embedding: EmbeddingIdentity | null;
}

export interface EmbeddingStatus {
  document_id: string;
  state: "unavailable" | "not_indexed" | "not_embedded" | "embedded" | "stale";
  chunk_count: number;
  embedded_count: number;
  stale_count: number;
  embedding: EmbeddingIdentity | null;
}

export const getRetrievalCapabilities = (projectId: string) =>
  request<RetrievalCapabilities>(
    `/api/projects/${projectId}/retrieval/capabilities`,
  );

export const getEmbeddingStatus = (projectId: string, documentId: string) =>
  request<EmbeddingStatus>(
    `${documentsPath(projectId)}/${documentId}/embedding-status`,
  );

export const embedDocument = (
  projectId: string,
  documentId: string,
  reembed: boolean,
) =>
  request<EmbeddingStatus & { action: string }>(
    `${documentsPath(projectId)}/${documentId}/embed`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reembed }),
    },
  );

export const getIndexStatus = (projectId: string, documentId: string) =>
  request<IndexStatus>(
    `${documentsPath(projectId)}/${documentId}/index-status`,
  );

export const indexDocument = (
  projectId: string,
  documentId: string,
  reindex: boolean,
) =>
  request<IndexResult>(`${documentsPath(projectId)}/${documentId}/index`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reindex }),
  });

export const searchDocuments = (
  projectId: string,
  query: string,
  documentId: string | null,
  mode: SearchMode = "keyword",
) =>
  request<SearchResponse>(`/api/projects/${projectId}/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query,
      document_id: documentId,
      top_k: 10,
      mode,
    }),
  });

export interface AIUsage {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
}

export interface AIGenerateResponse {
  answer: string;
  provider: string;
  model: string;
  latency_ms: number;
  usage: AIUsage;
}

export const AI_MAX_PROMPT_CHARS = 4000;

export function generateAI(prompt: string): Promise<AIGenerateResponse> {
  return request<AIGenerateResponse>("/api/ai/generate", {
    method: "POST",
    body: JSON.stringify({ prompt }),
  });
}

export type RequirementType =
  "business" | "functional" | "non_functional" | "constraint";
export type RequirementStatus = "proposed" | "confirmed" | "rejected";

export const REQUIREMENT_STATUSES: RequirementStatus[] = [
  "proposed",
  "confirmed",
  "rejected",
];

export interface Requirement {
  id: string;
  project_id: string;
  document_id: string;
  extraction_run_id: string;
  type: RequirementType;
  title: string;
  description: string;
  source_quote: string;
  source_start: number;
  source_end: number;
  status: RequirementStatus;
  created_at: string;
  updated_at: string;
}

export interface ExtractionRun {
  id: string;
  provider: string;
  model: string;
  prompt_version: string;
  status: string;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
}

export interface ExtractionResult {
  run: ExtractionRun;
  requirements: Requirement[];
  skipped_duplicates: number;
}

export interface RequirementPatch {
  title?: string;
  description?: string;
  status?: "confirmed" | "rejected";
}

const projectPath = (projectId: string) => `/api/projects/${projectId}`;

export const extractRequirements = (projectId: string, documentId: string) =>
  request<ExtractionResult>(
    `${projectPath(projectId)}/documents/${documentId}/extract-requirements`,
    { method: "POST" },
  );

export const listRequirements = (projectId: string) =>
  request<Requirement[]>(`${projectPath(projectId)}/requirements`);

export const updateRequirement = (
  projectId: string,
  id: string,
  patch: RequirementPatch,
) =>
  request<Requirement>(`${projectPath(projectId)}/requirements/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });

export type ProposalStatus = "draft" | "approved" | "rejected";
export type DecisionOrigin =
  | "required_by_client"
  | "preferred_by_client"
  | "recommended_by_ai"
  | "assumed"
  | "requires_clarification";
export type NodeType =
  | "client"
  | "api"
  | "service"
  | "database"
  | "storage"
  | "queue"
  | "cache"
  | "identity"
  | "external"
  | "other";

export interface Decision {
  /** Absent on proposals stored before coverage tracking. */
  id?: string | null;
  name: string;
  decision: string;
  rationale: string;
  origin: DecisionOrigin;
  supporting_requirement_ids: string[];
  /** Absent on proposals stored before retrieval-grounded generation. */
  source_passage_ids?: string[];
  alternatives_considered: string[];
  assumptions: string[];
  risks: string[];
}

export interface StackItem extends Decision {
  layer: string;
  technology: string;
}

export interface ArchComponent {
  id: string;
  name: string;
  type: NodeType;
  responsibility: string;
  technology: string | null;
  requirement_ids: string[];
  source_passage_ids?: string[];
}

export interface ArchRisk {
  description: string;
  severity: "low" | "medium" | "high";
  mitigation: string;
  requirement_ids: string[];
  source_passage_ids?: string[];
}

export interface DiagramNode {
  id: string;
  name: string;
  type: NodeType;
}

export interface DiagramEdge {
  source: string;
  target: string;
  relationship: string;
}

export type CoverageStatus = "addressed" | "partial" | "unresolved";

export interface CoverageEntry {
  requirement_id: string;
  related_ids: string[];
  verification: string;
  status: CoverageStatus;
  gap: string | null;
}

export interface PassageSource {
  chunk_id: string;
  document_id: string;
  filename: string;
  source_start: number;
  source_end: number;
}

export type RetrievalStatus =
  | "used"
  | "no_matches"
  | "not_indexed"
  | "no_documents"
  | "disabled";

export interface RetrievalInfo {
  status: RetrievalStatus;
  passages_provided: number;
  unindexed_documents: number;
  budget_chars: number;
  truncated: boolean;
}

export interface ArchitectureContent {
  solution_overview: string;
  recommended_stack: StackItem[];
  components: ArchComponent[];
  integrations: Decision[];
  data_architecture: Decision[];
  security_architecture: Decision[];
  non_functional_design: Decision[];
  risks: ArchRisk[];
  assumptions: {
    statement: string;
    requirement_ids: string[];
    source_passage_ids?: string[];
  }[];
  open_questions: {
    question: string;
    why_it_matters: string | null;
    requirement_ids: string[];
    source_passage_ids?: string[];
  }[];
  alternatives: {
    title: string;
    description: string;
    trade_offs: string;
    requirement_ids: string[];
    source_passage_ids?: string[];
  }[];
  implementation_phases: {
    name: string;
    objectives: string[];
    requirement_ids: string[];
  }[];
  diagram: { nodes: DiagramNode[]; edges: DiagramEdge[] };
  /** null or absent: proposal predates the matrix, so coverage was not recorded. */
  coverage_matrix?: CoverageEntry[] | null;
  /** Written by the service. Absent on proposals that predate retrieval. */
  source_passages?: PassageSource[];
  retrieval?: RetrievalInfo | null;
}

export interface ProposalSummary {
  id: string;
  project_id: string;
  title: string;
  summary: string;
  status: ProposalStatus;
  version: number;
  provider: string;
  model: string;
  prompt_version: string;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  created_at: string;
  updated_at: string;
  stale_requirement_ids: string[];
  /** Set on a version produced by "Revise with findings". */
  revised_from_id?: string | null;
  revised_from_version?: number | null;
  /** Findings the source version had when it was revised (the "before" count). */
  revised_from_findings?: number | null;
  /** Per finding type, after a revision; null/absent when not a revision. */
  revised_outcome?: RevisionOutcome | null;
}

export interface RevisionOutcome {
  rules: Record<
    string,
    { before: number; resolved: number; still_present: number; introduced?: number }
  >;
  resolved: number;
  still_present: number;
  introduced: number;
}

export type LintRule =
  | "origin_overclaim"
  | "preference_overclaim"
  | "completed_implementation_wording"
  | "coverage_all_addressed"
  | "frontend_only_backend_requirement"
  | "passages_not_cited"
  | "meta_open_question";

export interface LintFinding {
  rule: LintRule;
  severity: "warning" | "info";
  message: string;
  /** Schema path of the item concerned; null for the whole proposal. */
  path: string | null;
  terms: string[];
}

export interface LintReport {
  version: number;
  findings: LintFinding[];
}

export interface ArchitectureProposal extends ProposalSummary {
  content: ArchitectureContent;
  /** Advisory. null or absent: the draft predates the lint, so it was not run. */
  lint_findings?: LintReport | null;
}

export interface ProposalPatch {
  title?: string;
  summary?: string;
  status?: "approved" | "rejected";
}

const architecturePath = (projectId: string) =>
  `${projectPath(projectId)}/architecture`;

export const generateArchitecture = (projectId: string) =>
  request<ArchitectureProposal>(`${architecturePath(projectId)}/generate`, {
    method: "POST",
  });

export const listProposals = (projectId: string) =>
  request<ProposalSummary[]>(architecturePath(projectId));

export const getProposal = (projectId: string, id: string) =>
  request<ArchitectureProposal>(`${architecturePath(projectId)}/${id}`);

export const reviseProposal = (projectId: string, id: string) =>
  request<ArchitectureProposal>(`${architecturePath(projectId)}/${id}/revise`, {
    method: "POST",
  });

export interface GraphNode {
  id: string;
  kind: string;
  label: string;
  source_ref: string;
}

export interface GraphEdge {
  id: string;
  source_id: string;
  target_id: string;
  kind: string;
  evidence: string;
}

export interface GraphData {
  built: boolean;
  built_at: string | null;
  node_count: number;
  edge_count: number;
  truncated: boolean;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface GraphRebuild {
  node_count: number;
  edge_count: number;
  skipped_requirement_refs: number;
}

export interface ImpactItem {
  node: GraphNode;
  depth: number;
  via_kind: string;
  via_evidence: string;
  reached_from: string;
  via_hub: boolean;
}

export type ImpactMode = "direct" | "transitive";

export interface GraphImpact {
  root: GraphNode;
  mode: ImpactMode;
  max_depth: number;
  hub_threshold: number;
  truncated: boolean;
  items: ImpactItem[];
  hubs_not_expanded: { node: GraphNode; degree: number }[];
}

const graphPath = (projectId: string, id: string) =>
  `${architecturePath(projectId)}/${id}/graph`;

export const getGraph = (projectId: string, id: string) =>
  request<GraphData>(graphPath(projectId, id));

export const rebuildGraph = (projectId: string, id: string) =>
  request<GraphRebuild>(`${graphPath(projectId, id)}/rebuild`, { method: "POST" });

export const getGraphImpact = (
  projectId: string,
  id: string,
  nodeId: string,
  mode: ImpactMode = "direct",
) =>
  request<GraphImpact>(
    `${graphPath(projectId, id)}/impact?node_id=${encodeURIComponent(nodeId)}&mode=${mode}`,
  );

export const getGraphOrphans = (projectId: string, id: string) =>
  request<{ orphans: GraphNode[] }>(`${graphPath(projectId, id)}/orphans`);

export const updateProposal = (
  projectId: string,
  id: string,
  patch: ProposalPatch,
) =>
  request<ArchitectureProposal>(`${architecturePath(projectId)}/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
