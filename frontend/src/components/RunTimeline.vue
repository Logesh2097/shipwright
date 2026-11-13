<script setup lang="ts">
import { computed } from 'vue'

import type { AgentEvent, Json } from '@/api/types'
import { formatDuration, NODE_LABELS } from '@/lib/format'

const props = defineProps<{ events: AgentEvent[]; live: boolean }>()

interface Row {
  id: number
  time: string
  tone: 'neutral' | 'ok' | 'bad' | 'warn' | 'accent'
  title: string
  detail: string
}

function text(value: Json | undefined): string {
  if (value === undefined || value === null) return ''
  return typeof value === 'string' ? value : JSON.stringify(value)
}

function describe(event: AgentEvent): Row | null {
  const p = event.payload
  const base = { id: event.id, time: new Date(event.created_at).toLocaleTimeString(), detail: '' }
  const node = event.node ? (NODE_LABELS[event.node] ?? event.node) : ''
  switch (event.type) {
    case 'run_started':
      return { ...base, tone: 'accent', title: 'Run started' }
    case 'run_resumed':
      return { ...base, tone: 'accent', title: 'Run resumed' }
    case 'run_paused':
      return { ...base, tone: 'warn', title: 'Paused: waiting for your decision' }
    case 'run_interrupted':
      return { ...base, tone: 'warn', title: 'Interrupted', detail: text(p.reason) }
    case 'run_finished':
      return { ...base, tone: p.status === 'completed' ? 'ok' : p.status === 'failed' ? 'bad' : 'neutral', title: `Run ${text(p.status)}`, detail: text(p.error) }
    case 'node_started':
      return { ...base, tone: 'neutral', title: node }
    case 'node_finished': {
      const parts: string[] = []
      const c = p.classification as { task_type?: string } | undefined
      if (c?.task_type) parts.push(`type: ${c.task_type}`)
      if (Array.isArray(p.relevant_files)) parts.push(`candidates: ${(p.relevant_files as string[]).slice(0, 5).join(', ') || 'none'}`)
      if (p.plan_summary) parts.push(text(p.plan_summary))
      if (p.apply_status) parts.push(`apply: ${text(p.apply_status)}`)
      if (p.verification_status) parts.push(`verification: ${text(p.verification_status)}`)
      return parts.length ? { ...base, tone: 'neutral', title: `${node} — done`, detail: parts.join(' · ') } : null
    }
    case 'llm_call':
      return null
    case 'tool_call': {
      const args = p.arguments as Record<string, Json> | undefined
      const summary = args ? Object.entries(args).map(([k, v]) => `${k}=${text(v)}`).join(' ').slice(0, 160) : ''
      return {
        ...base,
        tone: p.ok ? 'neutral' : 'bad',
        title: `Tool: ${text(p.tool)}${p.ok ? '' : ` — ${text(p.error_code)}`}`,
        detail: `${summary}${typeof p.duration_ms === 'number' ? ` (${formatDuration(p.duration_ms)})` : ''}`,
      }
    }
    case 'security_notice':
      return { ...base, tone: 'warn', title: 'Security notice', detail: text(p.notice) }
    case 'change_set_created':
      return { ...base, tone: 'accent', title: 'Changes proposed', detail: (p.files as { path: string }[] | undefined)?.map((f) => f.path).join(', ') ?? '' }
    case 'approval_requested':
      return { ...base, tone: 'warn', title: 'Approval requested' }
    case 'approval_decided':
      return { ...base, tone: p.decision === 'approved' ? 'ok' : 'neutral', title: `Decision: ${text(p.decision)}` }
    case 'changes_applied':
      return { ...base, tone: p.status === 'applied' ? 'ok' : 'bad', title: `Apply result: ${text(p.status)}`, detail: ((p.applied as string[] | undefined) ?? []).join(', ') }
    case 'test_run': {
      const t = p.totals as Record<string, number> | undefined
      return {
        ...base,
        tone: p.status === 'passed' ? 'ok' : p.status === 'not_executed' ? 'warn' : 'bad',
        title: `${text(p.framework)} ${text(p.kind)}: ${text(p.status)}`,
        detail: t ? `${t.passed} passed, ${t.failed} failed, ${t.skipped} skipped, ${t.error} errors, ${t.not_executed} not executed` : '',
      }
    }
    case 'error':
      return { ...base, tone: 'bad', title: `Error in ${node || 'run'}`, detail: text(p.error) }
    default:
      return { ...base, tone: 'neutral', title: event.type }
  }
}

const rows = computed(() => props.events.map(describe).filter((row): row is Row => row !== null))
const DOT = { neutral: 'bg-line', ok: 'bg-ok', bad: 'bg-bad', warn: 'bg-warn', accent: 'bg-accent' } as const
</script>

<template>
  <ol class="space-y-0" aria-label="Agent activity" aria-live="polite">
    <li v-for="row in rows" :key="row.id" class="flex gap-3 py-1.5">
      <span class="mt-1.5 size-2 shrink-0 rounded-full" :class="DOT[row.tone]" aria-hidden="true"></span>
      <div class="min-w-0 flex-1">
        <p :class="{ 'text-bad': row.tone === 'bad', 'text-warn': row.tone === 'warn' }">{{ row.title }}</p>
        <p v-if="row.detail" class="font-mono text-xs break-words text-muted">{{ row.detail }}</p>
      </div>
      <time class="shrink-0 text-xs text-muted">{{ row.time }}</time>
    </li>
    <li v-if="live" class="flex items-center gap-3 py-1.5 text-muted">
      <span class="size-2 animate-pulse rounded-full bg-accent" aria-hidden="true"></span>
      Working…
    </li>
    <li v-if="!rows.length && !live" class="py-2 text-muted">No activity recorded for this run.</li>
  </ol>
</template>
