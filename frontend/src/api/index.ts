import { http, rawRequest } from './client'
import type * as T from './types'

type Page = { limit?: number; offset?: number }

export const api = {
  // --- auth and users
  register: (email: string, full_name: string, password: string) => http.post<T.User>('/auth/register', { email, full_name, password }),
  login: (email: string, password: string) => http.post<T.Tokens>('/auth/login', { email, password }),
  me: () => http.get<T.User>('/auth/me'),
  users: () => http.get<T.ListResponse<T.User>>('/users', { limit: 200 }),
  updateUser: (id: string, body: Partial<Pick<T.User, 'role' | 'is_active'>>) => http.patch<T.User>(`/users/${id}`, body),

  // --- projects
  projects: () => http.get<T.ListResponse<T.Project>>('/projects', { limit: 200 }),
  createProject: (name: string, description: string) => http.post<T.Project>('/projects', { name, description }),
  updateProject: (id: string, body: { name?: string; description?: string; settings?: { auto_run_tests: boolean } }) =>
    http.patch<T.Project>(`/projects/${id}`, body),
  deleteProject: (id: string) => http.delete<void>(`/projects/${id}`),
  members: (projectId: string) => http.get<T.Member[]>(`/projects/${projectId}/members`),
  upsertMember: (projectId: string, email: string, role: T.ProjectRole) => http.put<T.Member>(`/projects/${projectId}/members`, { email, role }),
  removeMember: (projectId: string, userId: string) => http.delete<void>(`/projects/${projectId}/members/${userId}`),

  // --- repositories
  repositories: (projectId: string) => http.get<T.Repository[]>(`/projects/${projectId}/repositories`),
  repository: (id: string) => http.get<T.Repository>(`/repositories/${id}`),
  registerRepository: (projectId: string, local_path: string, name?: string) =>
    http.post<T.Repository>(`/projects/${projectId}/repositories`, { local_path, name: name || null }),
  unregisterRepository: (id: string) => http.delete<void>(`/repositories/${id}`),
  reindex: (id: string) => http.post<T.Repository>(`/repositories/${id}/index`),
  tree: (id: string, path = '.') => http.get<{ path: string; entries: T.TreeEntry[]; truncated: boolean; hidden_entries: number }>(`/repositories/${id}/tree`, { path }),
  file: (id: string, path: string, start_line = 1) => http.get<T.FileContent>(`/repositories/${id}/file`, { path, start_line, max_lines: 800 }),
  symbols: (id: string, q: string, kind?: string) => http.get<{ items: T.SymbolHit[] }>(`/repositories/${id}/symbols`, { q, kind }),
  searchText: (id: string, q: string) =>
    http.get<{ matches: { path: string; line: number; text: string }[]; total_matches: number; truncated: boolean }>(`/repositories/${id}/search`, { q }),
  relevantFiles: (id: string, q: string) =>
    http.get<{ items: { path: string; score: number; language: string | null; lines: number; is_test: boolean; reasons: string[] }[] }>(
      `/repositories/${id}/relevant-files`,
      { q },
    ),
  routes: (id: string) => http.get<{ items: T.RouteHit[] }>(`/repositories/${id}/routes`),
  context: (id: string) => http.get<{ overview: string; profile: T.RepoProfile }>(`/repositories/${id}/context`),
  unusedCode: (id: string) => http.get<T.UnusedCode>(`/repositories/${id}/unused-code`),
  gitStatus: (id: string) => http.get<T.GitStatus>(`/repositories/${id}/git/status`),
  gitDiff: (id: string, path?: string) => http.get<{ diff: string; truncated: boolean; empty: boolean }>(`/repositories/${id}/git/diff`, { path }),
  gitLog: (id: string) => http.get<{ commits: { sha: string; author: string; date: string; subject: string }[] }>(`/repositories/${id}/git/log`),
  gitBranches: (id: string) =>
    http.get<{ current: string | null; branches: { name: string; commit: string; current: boolean; remote: boolean }[] }>(`/repositories/${id}/git/branches`),

  // --- tasks and runs
  createTask: (repository_id: string, request: string) => http.post<T.Task>('/tasks', { repository_id, request }),
  tasks: (query: Page & { project_id?: string; repository_id?: string; status?: string } = {}) => http.get<T.ListResponse<T.Task>>('/tasks', query),
  task: (id: string) => http.get<T.Task>(`/tasks/${id}`),
  cancelTask: (id: string) => http.post<T.Task>(`/tasks/${id}/cancel`),
  resumeTask: (id: string) => http.post<T.Task>(`/tasks/${id}/resume`),
  runEvents: (runId: string, after = 0) => http.get<T.AgentEvent[]>(`/runs/${runId}/events`, { after }),
  runStream: (runId: string, after: number, signal: AbortSignal) => rawRequest(`/runs/${runId}/stream`, { query: { after }, signal }),

  // --- change sets and approvals
  changeSets: (query: Page & { task_id?: string; repository_id?: string; status?: string } = {}) =>
    http.get<T.ListResponse<T.ChangeSetSummary>>('/change-sets', query),
  changeSet: (id: string) => http.get<T.ChangeSet>(`/change-sets/${id}`),
  staleness: (id: string) => http.get<{ applicable: boolean; stale_files: { path: string; reason: string }[] }>(`/change-sets/${id}/staleness`),
  revertChangeSet: (id: string) => http.post<T.ChangeSet>(`/change-sets/${id}/revert`),
  approvals: (query: Page & { status?: string; repository_id?: string; task_id?: string } = {}) => http.get<T.ListResponse<T.Approval>>('/approvals', query),
  approval: (id: string) => http.get<T.Approval>(`/approvals/${id}`),
  approve: (id: string, comment: string, confirm_deletions = false) =>
    http.post<T.Approval>(`/approvals/${id}/approve`, { comment: comment || null, confirm_deletions }),
  reject: (id: string, comment: string) => http.post<T.Approval>(`/approvals/${id}/reject`, { comment: comment || null }),

  // --- tests
  discoverTests: (repoId: string) =>
    http.get<{
      test_frameworks: { framework: string; directory: string }[]
      quality_tools: { tool: string; kind: string; directory: string }[]
      npm_scripts: Record<string, Record<string, string>>
      test_files: string[]
      test_file_count: number
    }>(`/repositories/${repoId}/tests/discover`),
  runCheck: (repoId: string, body: { kind: string; tool: string; targets: string[] }, directory = '.') =>
    http.post<T.TestRunDetail>(`/repositories/${repoId}/test-runs`, body, { directory }),
  testRuns: (query: Page & { repository_id?: string; task_id?: string } = {}) => http.get<T.ListResponse<T.TestRun>>('/test-runs', query),
  testRun: (id: string) => http.get<T.TestRunDetail>(`/test-runs/${id}`),

  // --- sensitive operations (each returns a pending approval)
  requestBranch: (repoId: string, name: string) => http.post<T.Approval>(`/repositories/${repoId}/git/branch`, { name }),
  requestCommit: (repoId: string, paths: string[], message: string) => http.post<T.Approval>(`/repositories/${repoId}/git/commit`, { paths, message }),
  requestPush: (repoId: string, branch: string) => http.post<T.Approval>(`/repositories/${repoId}/git/push`, { branch }),
  requestPullRequest: (repoId: string, body: { head: string; base?: string; title: string; body: string; draft: boolean }) =>
    http.post<T.Approval>(`/repositories/${repoId}/github/pull-requests`, body),
  requestCommand: (repoId: string, argv: string[], directory = '.') => http.post<T.Approval>(`/repositories/${repoId}/commands`, { argv }, { directory }),

  // --- github
  githubStatus: () => http.get<T.GitHubStatus>('/github/status'),
  setGithubToken: (token: string) => http.put<T.GitHubStatus>('/github/credential', { token }),
  deleteGithubToken: () => http.delete<void>('/github/credential'),
  githubRepos: () => http.get<{ repositories: T.GitHubRepo[] }>('/github/repositories'),
  githubIssues: (fullName: string) => http.get<{ issues: T.GitHubIssue[] }>(`/github/repositories/${fullName}/issues`),
  githubPulls: (fullName: string) => http.get<{ pull_requests: T.GitHubPull[] }>(`/github/repositories/${fullName}/pulls`),
  githubBranches: (fullName: string) => http.get<{ branches: { name: string; commit: string; protected: boolean }[] }>(`/github/repositories/${fullName}/branches`),
  githubCommits: (fullName: string) =>
    http.get<{ commits: { sha: string; message: string; author: string; date: string; html_url: string }[] }>(`/github/repositories/${fullName}/commits`),

  // --- system
  dashboard: () => http.get<T.DashboardSummary>('/dashboard/summary'),
  systemSettings: () => http.get<T.SystemSettings>('/settings/system'),
  mcpServers: () => http.get<T.McpServers>('/mcp/servers'),
  mcpTools: () => http.get<{ tools: T.McpTool[]; risk_levels: Record<string, string> }>('/mcp/tools'),
  toolExecutions: (query: Page & { repository_id?: string; run_id?: string; status?: string; tool?: string } = {}) =>
    http.get<T.ListResponse<T.ToolExecution>>('/tool-executions', query),
  auditLogs: (query: Page & { action?: string } = {}) => http.get<T.ListResponse<T.AuditLog>>('/audit-logs', query),
}
