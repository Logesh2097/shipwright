<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { api } from '@/api'
import { errorMessage } from '@/api/client'
import type { AgentEvent, Approval, ChangeSetSummary, Task, TestRun } from '@/api/types'
import ApprovalPanel from '@/components/ApprovalPanel.vue'
import AsyncBlock from '@/components/AsyncBlock.vue'
import MarkdownView from '@/components/MarkdownView.vue'
import NoticeBox from '@/components/NoticeBox.vue'
import PageHeader from '@/components/PageHeader.vue'
import RunTimeline from '@/components/RunTimeline.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import TestCounts from '@/components/TestCounts.vue'
import { formatDateTime, formatNumber, NODE_LABELS, titleCase } from '@/lib/format'
import { readSse } from '@/lib/sse'
import { useWorkspaceStore } from '@/stores/workspace'

const props = defineProps<{ id: string }>()
const workspace = useWorkspaceStore()

const task = ref<Task | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const events = ref<AgentEvent[]>([])
const approvals = ref<Approval[]>([])
const changeSets = ref<ChangeSetSummary[]>([])
const testRuns = ref<TestRun[]>([])
const actionError = ref<string | null>(null)
const acting = ref(false)
const tab = ref<'progress' | 'analysis' | 'report'>('progress')

let abort: AbortController | null = null
let timer: number | undefined
let disposed = false

const run = computed(() => task.value?.latest_run ?? null)
const live = computed(() => task.value?.status === 'running' || task.value?.status === 'queued')
const pendingApproval = computed(() => approvals.value.find((a) => a.status === 'pending') ?? null)
const securityNotices = computed(() => events.value.filter((e) => e.type === 'security_notice').map((e) => String(e.payload.notice ?? '')))

async function loadRelated(): Promise<void> {
  const [a, c, t] = await Promise.all([
    api.approvals({ task_id: props.id, limit: 50 }),
    api.changeSets({ task_id: props.id, limit: 50 }),
    api.testRuns({ task_id: props.id, limit: 50 }),
  ])
  approvals.value = a.items
  changeSets.value = c.items
  testRuns.value = t.items
}

async function loadTask(): Promise<void> {
  task.value = await api.task(props.id)
  if (task.value.latest_run?.final_report && tab.value === 'progress' && !live.value && task.value.status === 'completed') tab.value = 'report'
}

async function loadEvents(): Promise<void> {
  if (!run.value) return
  const after = events.value.at(-1)?.id ?? 0
  const fresh = await api.runEvents(run.value.id, after)
  if (fresh.length) events.value = [...events.value, ...fresh.filter((e) => e.id > after)]
}

async function refresh(): Promise<void> {
  try {
    await loadTask()
    await Promise.all([loadEvents(), loadRelated()])
    error.value = null
  } catch (cause) {
    if (!task.value) error.value = errorMessage(cause)
  }
}

/** Follow the run over SSE while it is executing; fall back to polling if the stream fails. */
async function follow(): Promise<void> {
  if (disposed || !run.value || !live.value) return
  abort?.abort()
  abort = new AbortController()
  try {
    const response = await api.runStream(run.value.id, events.value.at(-1)?.id ?? 0, abort.signal)
    await readSse(response, (message) => {
      if (message.event !== 'agent_event') return
      const event = JSON.parse(message.data) as AgentEvent
      if (!events.value.some((e) => e.id === event.id)) events.value = [...events.value, event]
      if (['node_started', 'approval_requested', 'change_set_created', 'test_run'].includes(event.type)) void loadTask().then(loadRelated)
    })
  } catch {
    // Stream unavailable or aborted: the interval below keeps the page current.
  }
  if (disposed) return
  await refresh()
  if (live.value) window.setTimeout(() => void follow(), 1500)
}

onMounted(async () => {
  await refresh()
  loading.value = false
  void follow()
  // Slow poll as a safety net (for example while waiting for approval, when no stream is open).
  timer = window.setInterval(() => {
    if (document.visibilityState !== 'visible') return
    const wasLive = live.value
    void refresh().then(() => {
      if (!wasLive && live.value) void follow()
    })
  }, 4000)
})
onBeforeUnmount(() => {
  disposed = true
  abort?.abort()
  window.clearInterval(timer)
})

async function act(action: () => Promise<Task>): Promise<void> {
  acting.value = true
  actionError.value = null
  try {
    task.value = await action()
    await refresh()
    void follow()
  } catch (cause) {
    actionError.value = errorMessage(cause)
  } finally {
    acting.value = false
  }
}

async function onDecided(): Promise<void> {
  await refresh()
  void follow()
}
</script>

<template>
  <AsyncBlock :loading="loading" :error="error" @retry="refresh">
    <template v-if="task">
      <PageHeader :title="task.title" :subtitle="`${workspace.repositoryName(task.repository_id)} · created ${formatDateTime(task.created_at)}`">
        <StatusBadge :status="task.status" />
        <button
          v-if="workspace.canWrite && ['queued', 'running', 'awaiting_approval', 'interrupted'].includes(task.status)"
          class="btn btn-sm"
          type="button"
          :disabled="acting"
          @click="act(() => api.cancelTask(task!.id))"
        >
          Cancel task
        </button>
        <button
          v-if="workspace.canWrite && ['failed', 'interrupted'].includes(task.status)"
          class="btn btn-sm btn-primary"
          type="button"
          :disabled="acting"
          @click="act(() => api.resumeTask(task!.id))"
        >
          Resume from last checkpoint
        </button>
      </PageHeader>

      <div class="space-y-3">
        <NoticeBox v-if="actionError" tone="bad">{{ actionError }}</NoticeBox>
        <NoticeBox v-if="run?.error" :tone="task.status === 'interrupted' ? 'warn' : 'bad'" :title="task.status === 'interrupted' ? 'Run interrupted' : 'Run failed'">
          {{ run.error }}
        </NoticeBox>
        <NoticeBox v-if="securityNotices.length" tone="warn" title="Content that looked like instructions to the agent was found">
          <ul class="list-disc pl-5">
            <li v-for="(notice, index) in [...new Set(securityNotices)]" :key="index">{{ notice }}</li>
          </ul>
          <p class="mt-1">It was treated as data. Tool permissions do not depend on what the model reads.</p>
        </NoticeBox>

        <template v-if="pendingApproval">
          <ApprovalPanel :approval="pendingApproval" @decided="onDecided" />
          <RouterLink v-if="pendingApproval.change_set_id" :to="`/changes/${pendingApproval.change_set_id}`" class="btn btn-primary">
            Review the exact diff before deciding
          </RouterLink>
        </template>
      </div>

      <section class="card mt-4 p-4">
        <h2>Request</h2>
        <p class="mt-1 whitespace-pre-wrap">{{ task.request_text }}</p>
        <dl v-if="run" class="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted">
          <div><dt class="inline">Type: </dt><dd class="inline text-fg">{{ titleCase(task.task_type) }}</dd></div>
          <div v-if="live && run.current_node"><dt class="inline">Step: </dt><dd class="inline text-fg">{{ NODE_LABELS[run.current_node] ?? run.current_node }}</dd></div>
          <div><dt class="inline">Model calls: </dt><dd class="inline text-fg">{{ run.llm_calls }}</dd></div>
          <div><dt class="inline">Tokens: </dt><dd class="inline text-fg">{{ formatNumber(run.input_tokens) }} in / {{ formatNumber(run.output_tokens) }} out</dd></div>
          <div v-if="run.repair_attempts"><dt class="inline">Repair attempts: </dt><dd class="inline text-fg">{{ run.repair_attempts }}</dd></div>
        </dl>
      </section>

      <div class="tabs mt-4" role="tablist">
        <button
          v-for="option in ([
            { key: 'progress', label: 'Progress' },
            { key: 'analysis', label: 'Analysis & plan' },
            { key: 'report', label: 'Report' },
          ] as const)"
          :key="option.key"
          type="button"
          role="tab"
          class="tab"
          :aria-selected="tab === option.key"
          @click="tab = option.key"
        >
          {{ option.label }}
        </button>
      </div>

      <div class="mt-4 grid gap-4 lg:grid-cols-3">
        <div class="lg:col-span-2">
          <section v-show="tab === 'progress'" class="card p-4" role="tabpanel">
            <h2 class="mb-2">Agent activity</h2>
            <RunTimeline :events="events" :live="live" />
          </section>

          <div v-show="tab === 'analysis'" class="space-y-4" role="tabpanel">
            <section class="card p-4">
              <h2>Analysis</h2>
              <MarkdownView v-if="run?.analysis" :source="run.analysis" class="mt-2" />
              <p v-else class="mt-2 text-muted">{{ live ? 'The agent has not finished analysing yet.' : 'No analysis was produced for this run.' }}</p>
            </section>
            <section class="card p-4">
              <h2>Plan</h2>
              <template v-if="run?.plan">
                <p class="mt-2">{{ run.plan.summary }}</p>
                <p class="mt-2 text-xs text-muted">{{ run.plan.requires_code_changes ? 'Requires code changes' : 'No code changes needed' }}</p>
                <ol v-if="run.plan.steps.length" class="mt-3 list-decimal space-y-2 pl-5">
                  <li v-for="(step, index) in run.plan.steps" :key="index">
                    {{ step.description }}
                    <span v-if="step.files.length" class="block font-mono text-xs text-muted">{{ step.files.join(', ') }}</span>
                  </li>
                </ol>
                <template v-if="run.plan.files_to_change.length">
                  <h3 class="mt-4">Files to change</h3>
                  <ul class="mt-1 font-mono text-xs"><li v-for="file in run.plan.files_to_change" :key="file">{{ file }}</li></ul>
                </template>
                <template v-if="run.plan.tests_to_run.length">
                  <h3 class="mt-4">Tests to run</h3>
                  <ul class="mt-1 font-mono text-xs"><li v-for="file in run.plan.tests_to_run" :key="file">{{ file }}</li></ul>
                </template>
                <template v-if="run.plan.risks.length">
                  <h3 class="mt-4">Risks</h3>
                  <ul class="mt-1 list-disc pl-5"><li v-for="(risk, index) in run.plan.risks" :key="index">{{ risk }}</li></ul>
                </template>
              </template>
              <p v-else class="mt-2 text-muted">{{ live ? 'The plan is not ready yet.' : 'No plan was produced for this run.' }}</p>
            </section>
          </div>

          <section v-show="tab === 'report'" class="card p-4" role="tabpanel">
            <h2>Final report</h2>
            <MarkdownView v-if="run?.final_report" :source="run.final_report" class="mt-2" />
            <p v-else class="mt-2 text-muted">
              {{ live || task.status === 'awaiting_approval' ? 'The report is written when the run finishes.' : 'This run ended without a report.' }}
            </p>
          </section>
        </div>

        <aside class="space-y-4">
          <section class="card">
            <h2 class="card-head">Proposed changes</h2>
            <p v-if="!changeSets.length" class="p-4 text-muted">No changes have been proposed.</p>
            <ul v-else>
              <li v-for="changeSet in changeSets" :key="changeSet.id" class="border-b border-line last:border-b-0">
                <RouterLink :to="`/changes/${changeSet.id}`" class="block px-4 py-2.5 hover:bg-raised">
                  <span class="flex items-center justify-between gap-2">
                    <span class="truncate font-medium">{{ changeSet.title }}</span>
                    <StatusBadge :status="changeSet.status" />
                  </span>
                  <span class="text-xs text-muted">{{ changeSet.file_count }} file(s) · open to see the diff</span>
                </RouterLink>
              </li>
            </ul>
          </section>

          <section class="card">
            <h2 class="card-head">Verification</h2>
            <p v-if="!testRuns.length" class="p-4 text-muted">No tests or checks have been run for this task.</p>
            <ul v-else>
              <li v-for="testRun in testRuns" :key="testRun.id" class="border-b border-line px-4 py-2.5 last:border-b-0">
                <RouterLink :to="{ path: '/tests', query: { run: testRun.id } }" class="flex items-center justify-between gap-2 hover:underline">
                  <span class="font-medium">{{ testRun.framework }} · {{ testRun.kind }}</span>
                  <StatusBadge :status="testRun.status" />
                </RouterLink>
                <TestCounts
                  class="mt-1"
                  :passed="testRun.passed"
                  :failed="testRun.failed"
                  :skipped="testRun.skipped"
                  :errors="testRun.errors"
                  :not-executed="testRun.not_executed"
                />
              </li>
            </ul>
          </section>

          <section v-if="approvals.some((a) => a.status !== 'pending')" class="card">
            <h2 class="card-head">Decisions</h2>
            <ul>
              <li v-for="approval in approvals.filter((a) => a.status !== 'pending')" :key="approval.id" class="border-b border-line px-4 py-2.5 last:border-b-0">
                <span class="flex items-center justify-between gap-2">
                  <span class="truncate">{{ approval.title }}</span>
                  <StatusBadge :status="approval.status" />
                </span>
                <span class="text-xs text-muted">{{ formatDateTime(approval.decided_at) }}<template v-if="approval.comment"> · “{{ approval.comment }}”</template></span>
              </li>
            </ul>
          </section>
        </aside>
      </div>
    </template>
  </AsyncBlock>
</template>
