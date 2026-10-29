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

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(message: string, status: number, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

interface PatchFailure {
  code: string;
  path?: string | null;
}

function describeDetail(detail: object): string | null {
  const d = detail as {
    message?: unknown;
    failures?: unknown;
    paths?: unknown;
  };
  if (typeof d.message !== "string") return null;
  let text = d.message;
  if (Array.isArray(d.failures)) {
    const items = (d.failures as PatchFailure[])
      .slice(0, 20)
      .map((f) => (f.path ? `${f.code} (${f.path})` : f.code));
    if (items.length > 0) text += ` [${items.join(", ")}]`;
  }
  if (Array.isArray(d.paths) && d.paths.length > 0) {
    text += ` [${(d.paths as string[]).slice(0, 20).join(", ")}]`;
  }
  return text;
}

async function errorInfo(
  response: Response,
): Promise<{ message: string; detail: unknown }> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string")
      return { message: body.detail, detail: body.detail };
    if (Array.isArray(body.detail)) {
      return {
        message: body.detail
          .map((d) =>
            typeof d === "object" && d && "msg" in d
              ? String(d.msg)
              : "invalid value",
          )
          .join("; "),
        detail: body.detail,
      };
    }
    if (body.detail && typeof body.detail === "object") {
      const described = describeDetail(body.detail);
      if (described) return { message: described, detail: body.detail };
    }
  } catch {
    // fall through to generic message
  }
  return { message: `Request failed (${response.status})`, detail: null };
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
    const { message, detail } = await errorInfo(response);
    // A 404 without a JSON "detail" does not come from this backend's router (FastAPI always
    // sends one), which usually means another service is listening on the API address.
    if (response.status === 404 && message.startsWith("Request failed")) {
      throw new Error(
        `The server at ${API_URL} has no ${path} endpoint (404). ` +
          "Check that the ArchPilotAI backend is running on this address.",
      );
    }
    throw new ApiError(message, response.status, detail);
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
  "used" | "no_matches" | "not_indexed" | "no_documents" | "disabled";

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
    {
      before: number;
      resolved: number;
      still_present: number;
      introduced?: number;
    }
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
  request<GraphRebuild>(`${graphPath(projectId, id)}/rebuild`, {
    method: "POST",
  });

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

export type PlanStatus = "draft" | "approved" | "rejected";

export interface PlanDependency {
  key: string;
  source: "diagram" | "nfr rule" | "manual";
  reason: string | null;
  author: string | null;
  created_at: string | null;
}

export interface ManualEdit {
  action: "add" | "remove";
  task_key: string;
  depends_on_key: string;
  reason: string;
  author: string;
  created_at: string;
}

export interface PlanTask {
  id: string;
  key: string;
  title: string;
  kind: string;
  description: string;
  source_ref: string;
  component_ref: string | null;
  requirement_ids: string[];
  acceptance_criteria: string[];
  depends_on: string[];
  dependencies: PlanDependency[];
  flags: string[];
  blocking_questions: string[];
  notes: string[];
  related: string[];
  order_index: number;
  status: string;
}

export interface PlanSummary {
  id: string;
  project_id: string;
  proposal_id: string;
  version: number;
  status: PlanStatus;
  generated_at: string;
  source_graph_built_at: string | null;
  task_count: number;
}

export interface ImplementationPlan extends PlanSummary {
  unattached_questions: string[];
  not_for_client: string[];
  dependency_count: number;
  unmapped_node_count: number;
  warnings: string[];
  manual_edits: ManualEdit[];
  previous_version: number | null;
  previous_manual_edits: ManualEdit[];
  tasks: PlanTask[];
}

export const editPlanDependency = (
  projectId: string,
  planId: string,
  action: "add" | "remove",
  body: {
    task_id: string;
    depends_on_task_id: string;
    reason: string;
    author: string;
  },
) =>
  request<ImplementationPlan>(
    `${projectPath(projectId)}/plans/${planId}/dependencies${action === "remove" ? "/remove" : ""}`,
    { method: "POST", body: JSON.stringify(body) },
  );

const plansPath = (projectId: string) => `${projectPath(projectId)}/plans`;

export const listPlans = (projectId: string) =>
  request<PlanSummary[]>(plansPath(projectId));

export const getPlan = (projectId: string, id: string) =>
  request<ImplementationPlan>(`${plansPath(projectId)}/${id}`);

export const buildPlan = (projectId: string) =>
  request<ImplementationPlan>(plansPath(projectId), { method: "POST" });

export const decidePlan = (
  projectId: string,
  id: string,
  decision: "approve" | "reject",
) =>
  request<ImplementationPlan>(`${plansPath(projectId)}/${id}/${decision}`, {
    method: "POST",
  });

export type PatchStatus = "proposed" | "accepted" | "rejected";

export interface PatchFileView {
  path: string;
  change: "create" | "modify" | "delete";
  content: string;
  previous_content_hash: string | null;
  diff: string;
  diff_truncated: boolean;
}

export interface PatchSummary {
  id: string;
  task_key: string;
  version: number;
  status: PatchStatus;
  provider: string;
  model: string;
  prompt_version: string;
  created_at: string;
  reviewed_by: string | null;
  review_reason: string | null;
  file_count: number;
  based_on_version?: number | null;
}

export interface CoverageItemView {
  kind: "criterion" | "requirement";
  ref: string;
  text: string;
  status: "covered" | "missing" | "unchecked";
  covered: string[];
  missing: string[];
  reason?: string | null;
  prompt?: string | null;
}

export interface PatchCoverage {
  items: CoverageItemView[];
  covered: number;
  missing: number;
  unchecked: number;
  removed?: { since: number; items: string[] };
  scope?: string[];
}

export interface CodePatchDetail extends PatchSummary {
  coverage?: PatchCoverage | null;
  plan_id: string;
  task_id: string;
  notes: string[];
  assumptions: string[];
  test_suggestions: string[];
  warnings: string[];
  blocking_acknowledged: boolean;
  validation: "passed" | "stale";
  stale: { code: string; path: string | null }[];
  files: PatchFileView[];
}

export interface TechnologyConfirmation {
  task_key: string;
  name: string;
  reason: string;
  created_at: string;
}

export const DECISION_AREAS = [
  "backend",
  "frontend",
  "database",
  "hosting",
  "auth",
  "testing",
  "style",
] as const;

export interface ProjectDecision {
  id: string;
  area: string;
  choice: string;
  reason: string;
  decided_by: string;
  decided_at: string;
  superseded_by: string | null;
}

export interface DecisionsOverview {
  areas: string[];
  current: ProjectDecision[];
  history: ProjectDecision[];
  suggestions: Record<string, { choice: string; layer: string; label: string }>;
}

export const getDecisions = (projectId: string) =>
  request<DecisionsOverview>(`${projectPath(projectId)}/decisions`);

export const saveDecision = (
  projectId: string,
  body: { area: string; choice: string; reason: string; name: string },
) =>
  request<ProjectDecision>(`${projectPath(projectId)}/decisions`, {
    method: "POST",
    body: JSON.stringify(body),
  });

export interface WorkspaceFileInfo {
  path: string;
  content_hash: string;
  size: number;
  source_patch_id: string;
}

export interface ExportResult {
  folder: string;
  written: string[];
  unchanged: string[];
  overwritten: string[];
  left_on_disk: string[];
  foreign_untouched: number;
}

const taskPath = (projectId: string, planId: string, key: string) =>
  `${plansPath(projectId)}/${planId}/tasks/${key}`;

export const listTechnologyConfirmations = (
  projectId: string,
  planId: string,
  key: string,
) =>
  request<TechnologyConfirmation[]>(
    `${taskPath(projectId, planId, key)}/technology-confirmation`,
  );

export const confirmTechnology = (
  projectId: string,
  planId: string,
  key: string,
  body: { name: string; reason: string },
) =>
  request<TechnologyConfirmation>(
    `${taskPath(projectId, planId, key)}/technology-confirmation`,
    { method: "POST", body: JSON.stringify(body) },
  );

export const listPatches = (projectId: string, planId: string, key: string) =>
  request<PatchSummary[]>(`${taskPath(projectId, planId, key)}/patches`);

export const generatePatch = (
  projectId: string,
  planId: string,
  key: string,
  acknowledgeBlockingQuestions: boolean,
) =>
  request<CodePatchDetail>(`${taskPath(projectId, planId, key)}/patches`, {
    method: "POST",
    body: JSON.stringify({
      acknowledge_blocking_questions: acknowledgeBlockingQuestions,
    }),
  });

export const getPatch = (projectId: string, patchId: string) =>
  request<CodePatchDetail>(`${projectPath(projectId)}/patches/${patchId}`);

export const reviewPatch = (
  projectId: string,
  patchId: string,
  action: "accept" | "reject",
  body: { reviewer: string; reason: string },
) =>
  request<CodePatchDetail>(
    `${projectPath(projectId)}/patches/${patchId}/${action}`,
    { method: "POST", body: JSON.stringify(body) },
  );

export const getWorkspace = (projectId: string) =>
  request<{ files: WorkspaceFileInfo[] }>(
    `${projectPath(projectId)}/workspace`,
  );

export const exportWorkspace = (projectId: string, confirmOverwrite: boolean) =>
  request<ExportResult>(`${projectPath(projectId)}/workspace/export`, {
    method: "POST",
    body: JSON.stringify({ confirm_overwrite: confirmOverwrite }),
  });

export type AgentRunStatus = "running" | "finished" | "stopped" | "failed";

export interface AgentStepView {
  step_no: number;
  action: string;
  path: string | null;
  status: "ok" | "error";
  error_code: string | null;
  result_summary: string;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  findings: AgentFinding[];
  finding_total: number | null;
  findings_not_shown: number;
  environment_reasons?: Record<string, number>;
  scope_counts?: Record<string, number>;
  tests?: AgentTestsView | null;
}

export interface AgentTestsView {
  command_id: string;
  status: string;
  structured: boolean;
  new: { name: string; message: string; file: string | null }[];
  new_in_changed: number;
  new_elsewhere: number;
  pre_existing: number;
  fixed: number;
  flaky: string[];
  baseline: boolean;
  note: string | null;
}

export interface AgentJob {
  id: string;
  command_id: string;
  variant: "baseline" | "changed" | "rerun";
  status: string;
  timeout_seconds: number;
  exit_code: number | null;
  duration_ms: number;
  truncated: boolean;
  select_test: string | null;
  flaky: boolean | null;
}

export interface AgentFinding {
  tool: string;
  file: string;
  line: number;
  code: string;
  message: string;
  scope?: string | null;
}

export interface AgentRunSummary {
  id: string;
  task_key: string;
  status: AgentRunStatus;
  stop_reason: string | null;
  stop_requested: boolean;
  max_steps: number;
  steps_taken: number;
  token_budget: number;
  input_tokens: number;
  output_tokens: number;
  time_limit_seconds: number;
  provider: string;
  model: string;
  prompt_version: string;
  summary: string | null;
  patch_id: string | null;
  started_at: string;
  finished_at: string | null;
  duration_ms: number;
}

export interface AgentRun extends AgentRunSummary {
  steps: AgentStepView[];
  jobs?: AgentJob[];
}

export const startAgentRun = (
  projectId: string,
  planId: string,
  key: string,
  acknowledgeBlockingQuestions: boolean,
) =>
  request<AgentRun>(`${taskPath(projectId, planId, key)}/agent-runs`, {
    method: "POST",
    body: JSON.stringify({
      acknowledge_blocking_questions: acknowledgeBlockingQuestions,
    }),
  });

export const listAgentRuns = (projectId: string, planId: string, key: string) =>
  request<AgentRunSummary[]>(`${taskPath(projectId, planId, key)}/agent-runs`);

export const getAgentRun = (projectId: string, runId: string) =>
  request<AgentRun>(`${projectPath(projectId)}/agent-runs/${runId}`);

export const stopAgentRun = (projectId: string, runId: string) =>
  request<AgentRun>(`${projectPath(projectId)}/agent-runs/${runId}/stop`, {
    method: "POST",
  });

export interface ChecksStatus {
  ok: boolean;
  tools: Record<string, { available: boolean; version: string | null }>;
}

export const getChecksStatus = () =>
  request<ChecksStatus>("/api/agent/checks/status");
