// Shapes returned by the backend (see docs/api-documentation.md and /api/openapi.json).

export type Json = string | number | boolean | null | Json[] | { [key: string]: Json }

export interface ListResponse<T> {
  items: T[]
  total: number
}

export interface User {
  id: string
  email: string
  full_name: string
  role: 'admin' | 'developer' | 'viewer'
  is_active: boolean
  created_at: string
}

export interface Tokens {
  access_token: string
  refresh_token: string
  expires_in: number
}

export type ProjectRole = 'owner' | 'developer' | 'viewer'

export interface Project {
  id: string
  name: string
  description: string
  owner_id: string
  settings: { auto_run_tests?: boolean }
  created_at: string
  my_role: ProjectRole | null
  repository_count: number
}

export interface Member {
  user_id: string
  email: string
  full_name: string
  role: ProjectRole
}

export interface RepoProfile {
  kind?: string
  languages?: Record<string, number>
  primary_language?: string | null
  frameworks?: string[]
  package_managers?: string[]
  test_frameworks?: { framework: string; directory: string }[]
  quality_tools?: { tool: string; kind: string; directory: string }[]
  entry_points?: string[]
  file_count?: number
  code_file_count?: number
  test_file_count?: number
  truncated?: boolean
}

export interface Repository {
  id: string
  project_id: string
  name: string
  local_path: string
  default_branch: string | null
  remote_url: string | null
  github_full_name: string | null
  status: 'registered' | 'indexing' | 'ready' | 'error'
  profile: RepoProfile
  head_commit: string | null
  last_indexed_at: string | null
  index_error: string | null
  created_at: string
}

export interface Plan {
  summary: string
  requires_code_changes: boolean
  steps: { description: string; files: string[] }[]
  files_to_change: string[]
  tests_to_run: string[]
  risks: string[]
}

export interface Classification {
  task_type: string
  needs_code_changes: boolean
  needs_tests: boolean
  search_terms: string[]
  summary: string
}

export interface Run {
  id: string
  task_id: string
  status: string
  current_node: string | null
  classification: Classification | null
  analysis: string | null
  plan: Plan | null
  final_report: string | null
  error: string | null
  llm_calls: number
  input_tokens: number
  output_tokens: number
  repair_attempts: number
  started_at: string | null
  finished_at: string | null
  created_at: string
}

export interface Task {
  id: string
  project_id: string
  repository_id: string
  created_by: string | null
  title: string
  request_text: string
  task_type: string | null
  status: string
  result_summary: string | null
  created_at: string
  updated_at: string
  latest_run: Run | null
}

export interface AgentEvent {
  id: number
  run_id: string
  type: string
  node: string | null
  payload: Record<string, Json>
  created_at: string
}

export interface FileChange {
  id: string
  path: string
  change_type: 'create' | 'modify' | 'delete'
  base_hash: string | null
  new_hash: string | null
  diff: string
  status: string
  error: string | null
}

export interface ChangeSetSummary {
  id: string
  run_id: string | null
  task_id: string | null
  repository_id: string
  title: string
  summary: string
  status: string
  created_at: string
  applied_at: string | null
  file_count: number
}

export interface ChangeSet extends Omit<ChangeSetSummary, 'file_count'> {
  base_commit: string | null
  decided_at: string | null
  files: FileChange[]
}

export interface Approval {
  id: string
  repository_id: string
  task_id: string | null
  run_id: string | null
  change_set_id: string | null
  action_type: string
  title: string
  payload: Record<string, Json>
  status: string
  requested_by_user_id: string | null
  decided_by: string | null
  decided_at: string | null
  comment: string | null
  result: Record<string, Json> | null
  expires_at: string | null
  created_at: string
}

export interface TestRun {
  id: string
  repository_id: string
  task_id: string | null
  run_id: string | null
  kind: string
  framework: string
  command: string[]
  targets: string[]
  status: string
  passed: number
  failed: number
  skipped: number
  errors: number
  not_executed: number
  duration_ms: number
  output_excerpt: string
  created_at: string
}

export interface TestCase {
  id: string
  node_id: string
  file: string | null
  name: string
  outcome: 'passed' | 'failed' | 'skipped' | 'error' | 'not_executed'
  duration_ms: number
  message: string | null
}

export interface TestRunDetail extends TestRun {
  cases: TestCase[]
}

export interface ToolExecution {
  id: string
  run_id: string | null
  repository_id: string | null
  user_id: string | null
  approval_id: string | null
  server: string
  tool_name: string
  risk_level: string
  arguments: Record<string, Json>
  status: string
  result_preview: string | null
  error: string | null
  duration_ms: number
  created_at: string
}

export interface AuditLog {
  id: string
  actor_user_id: string | null
  actor_type: string
  action: string
  resource_type: string | null
  resource_id: string | null
  details: Record<string, Json>
  ip_address: string | null
  created_at: string
}

export interface SandboxStatus {
  mode: string
  detail: string
  configured: string
}

export interface DashboardSummary {
  projects: number
  repositories: number
  tasks_by_status: Record<string, number>
  tasks_by_type: Record<string, number>
  pending_approvals: number
  change_sets_by_status: Record<string, number>
  test_runs_by_status: Record<string, number>
  tool_calls_by_status: Record<string, number>
  llm_usage: { calls: number; input_tokens: number; output_tokens: number }
  recent_tasks: { id: string; title: string; status: string; task_type: string | null; created_at: string }[]
  system: { llm_configured: boolean; sandbox: SandboxStatus; active_runs: number; mcp_sessions: number }
}

export interface SystemSettings {
  environment: string
  llm: { configured: boolean; model: string | null; provider: string }
  sandbox: SandboxStatus & { image: string; memory_mb: number; cpus: number; pids_limit: number }
  repositories: { allowed_roots: string[]; max_file_bytes: number }
  agent: { max_tool_steps: number; max_repair_attempts: number }
  approvals: { expiry_hours: number; token_ttl_seconds: number; actions: Record<string, string> }
  registration_open: boolean
  risk_levels: Record<string, string>
  command_policy_examples: { command: string; level: string; reason: string }[]
  roles: Record<string, string>
}

export interface McpSession {
  kind: string
  repository_id: string | null
  repository_name: string | null
  user_id: string | null
  transport: string
  alive: boolean
  tool_count: number
  calls: number
  errors: number
  started_at: number
  idle_seconds: number
}

export interface McpServers {
  sessions: McpSession[]
  restarts: number
  sandbox: SandboxStatus
  definitions: { kind: string; toolsets: string[]; transport: string; scope: string }[]
}

export interface McpTool {
  name: string
  server: string
  risk: string
  requires_approval: boolean
  description: string | null
  input_schema: { properties?: Record<string, unknown>; required?: string[] } | null
}

export interface TreeEntry {
  name: string
  path: string
  type: 'dir' | 'file'
  size?: number
}

export interface FileContent {
  path: string
  sha256: string
  total_lines: number
  start_line: number
  end_line: number
  truncated: boolean
  redacted: boolean
  content: string
}

export interface SymbolHit {
  name: string
  qualified_name: string
  kind: string
  path: string
  start_line: number
  end_line: number
  signature: string
}

export interface RouteHit {
  method: string
  path: string
  handler: string
  framework: string
  file: string
  line: number
}

export interface UnusedCode {
  unused_imports: { path: string; line: number; name: string; statement: string; confidence: string }[]
  unused_definitions: { path: string; line: number; name: string; kind: string; confidence: string }[]
  files_analysed: number
  notes: string[]
}

export interface GitStatus {
  branch: string | null
  tracking: string | null
  clean: boolean
  entries: { path: string; index: string; worktree: string; state: string }[]
}

export interface GitHubStatus {
  configured: boolean
  hint: string | null
  login: string | null
  updated_at: string | null
}

export interface GitHubRepo {
  full_name: string
  private: boolean
  description: string | null
  default_branch: string
  language: string | null
  html_url: string
  open_issues_count: number
  pushed_at: string | null
}

export interface GitHubIssue {
  number: number
  title: string
  state: string
  user: string | null
  labels: string[]
  html_url: string
  updated_at: string
}

export interface GitHubPull {
  number: number
  title: string
  state: string
  draft: boolean
  user: string | null
  head: string
  base: string
  html_url: string
  updated_at: string
}
