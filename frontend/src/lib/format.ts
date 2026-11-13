export function formatDateTime(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function formatRelative(value: string | null | undefined, now: Date = new Date()): string {
  if (!value) return '—'
  const then = new Date(value).getTime()
  if (Number.isNaN(then)) return '—'
  const seconds = Math.round((now.getTime() - then) / 1000)
  if (seconds < 45) return 'just now'
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`
  if (seconds < 86400 * 14) return `${Math.round(seconds / 86400)} d ago`
  return new Date(value).toLocaleDateString(undefined, { dateStyle: 'medium' })
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`
  return `${Math.floor(ms / 60_000)} min ${Math.round((ms % 60_000) / 1000)} s`
}

export function formatNumber(value: number): string {
  return value.toLocaleString()
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : '—'
}

export function titleCase(value: string | null | undefined): string {
  if (!value) return '—'
  const text = value.replace(/[_-]+/g, ' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

export type Tone = 'neutral' | 'ok' | 'warn' | 'bad' | 'info' | 'accent'

const STATUS_TONES: Record<string, Tone> = {
  // tasks and runs
  queued: 'neutral', running: 'accent', awaiting_approval: 'warn', completed: 'ok', failed: 'bad',
  cancelled: 'neutral', interrupted: 'warn',
  // change sets and file changes
  draft: 'neutral', proposed: 'warn', approved: 'info', rejected: 'neutral', applied: 'ok', stale: 'bad',
  reverted: 'neutral', partially_reverted: 'warn', revert_conflict: 'bad', pending: 'warn',
  // approvals
  executed: 'ok', expired: 'neutral',
  // tests
  passed: 'ok', skipped: 'neutral', error: 'bad', timeout: 'bad', not_executed: 'warn',
  // tools
  success: 'ok', denied: 'bad',
  // repositories
  ready: 'ok', indexing: 'accent', registered: 'neutral',
  // risk
  read: 'neutral', execute: 'info', write: 'warn', sensitive: 'bad',
  // command policy
  safe: 'ok',
}

export function toneFor(status: string | null | undefined): Tone {
  return (status && STATUS_TONES[status]) || 'neutral'
}

export const ACTIVE_STATUSES = new Set(['queued', 'running'])

export const NODE_LABELS: Record<string, string> = {
  intake: 'Checking the request',
  classify: 'Classifying the task',
  retrieve_context: 'Finding relevant files',
  analyze: 'Analysing the code',
  plan: 'Planning',
  propose_changes: 'Preparing changes',
  request_approval: 'Requesting approval',
  await_approval: 'Waiting for your decision',
  apply_changes: 'Applying approved changes',
  verify: 'Running tests and checks',
  repair: 'Repairing after failed verification',
  respond: 'Writing the report',
}
