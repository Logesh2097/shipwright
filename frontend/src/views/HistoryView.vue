<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'

import { api } from '@/api'
import AsyncBlock from '@/components/AsyncBlock.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import TablePager from '@/components/TablePager.vue'
import { formatDateTime, formatDuration, formatNumber, titleCase } from '@/lib/format'
import { PAGE_SIZE } from '@/lib/paging'
import { useAsync } from '@/lib/useAsync'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'

const workspace = useWorkspaceStore()
const auth = useAuthStore()
const tab = ref<'tasks' | 'audit'>('tasks')

// --- tasks
const status = ref('')
const scope = ref<'project' | 'all'>('project')
const taskPage = ref(0)
const tasks = useAsync(() =>
  api.tasks({
    status: status.value || undefined,
    project_id: scope.value === 'project' ? (workspace.projectId ?? undefined) : undefined,
    limit: PAGE_SIZE,
    offset: taskPage.value * PAGE_SIZE,
  }),
)
watch([status, scope, () => workspace.projectId], () => {
  taskPage.value = 0
  void tasks.load()
})
watch(taskPage, () => void tasks.load())

function runDuration(started: string | null, finished: string | null): string {
  if (!started || !finished) return '—'
  return formatDuration(new Date(finished).getTime() - new Date(started).getTime())
}

// --- audit (administrators)
const action = ref('')
const auditPage = ref(0)
const audit = useAsync(() => api.auditLogs({ action: action.value || undefined, limit: PAGE_SIZE, offset: auditPage.value * PAGE_SIZE }))
watch(action, () => {
  auditPage.value = 0
  void audit.load()
})
watch(auditPage, () => void audit.load())
watch(tab, (value) => {
  if (value === 'audit' && !audit.data.value) void audit.load()
})

onMounted(tasks.load)
</script>

<template>
  <PageHeader title="Execution history" subtitle="Every task the agent ran, with its outcome and cost, and (for administrators) the audit trail." />

  <div class="tabs mb-4" role="tablist">
    <button
      type="button"
      role="tab"
      class="tab"
      :aria-selected="tab === 'tasks'"
      @click="tab = 'tasks'"
    >
      Tasks and runs
    </button>
    <button
      v-if="auth.isAdmin"
      type="button"
      role="tab"
      class="tab"
      :aria-selected="tab === 'audit'"
      @click="tab = 'audit'"
    >
      Audit log
    </button>
  </div>

  <section v-if="tab === 'tasks'" class="card" role="tabpanel">
    <div class="card-head card-head--plain flex flex-wrap items-center gap-2">
      <select v-model="scope" class="input input-sm w-auto" aria-label="Scope">
        <option value="project">Selected project</option>
        <option value="all">All my projects</option>
      </select>
      <select v-model="status" class="input input-sm w-auto" aria-label="Status">
        <option value="">Any status</option>
        <option v-for="option in ['running', 'awaiting_approval', 'completed', 'failed', 'cancelled', 'interrupted']" :key="option" :value="option">
          {{ option.replace('_', ' ') }}
        </option>
      </select>
    </div>
    <AsyncBlock
      :loading="tasks.loading.value"
      :error="tasks.error.value"
      :empty="!tasks.data.value?.items.length"
      empty-title="No tasks match"
      empty-text="Tasks are created from the Developer assistant screen."
      @retry="tasks.load"
    >
      <div class="overflow-x-auto">
        <table class="table">
          <thead>
            <tr><th>Task</th><th>Type</th><th>Status</th><th>Outcome</th><th>Model calls</th><th>Tokens</th><th>Duration</th><th>Started</th></tr>
          </thead>
          <tbody>
            <tr v-for="task in tasks.data.value?.items ?? []" :key="task.id">
              <td class="max-w-xs">
                <RouterLink :to="`/tasks/${task.id}`" class="link block truncate font-medium">{{ task.title }}</RouterLink>
                <span class="text-xs text-muted">{{ workspace.repositoryName(task.repository_id) }}</span>
              </td>
              <td>{{ titleCase(task.task_type) }}</td>
              <td><StatusBadge :status="task.status" /></td>
              <td class="max-w-xs text-xs">
                <span v-if="task.latest_run?.error" class="text-bad">{{ task.latest_run.error.slice(0, 140) }}</span>
                <span v-else>{{ task.result_summary ?? '—' }}</span>
              </td>
              <td>{{ task.latest_run?.llm_calls ?? 0 }}</td>
              <td class="text-xs whitespace-nowrap">
                {{ formatNumber(task.latest_run?.input_tokens ?? 0) }} in<br />{{ formatNumber(task.latest_run?.output_tokens ?? 0) }} out
              </td>
              <td class="text-xs whitespace-nowrap">{{ runDuration(task.latest_run?.started_at ?? null, task.latest_run?.finished_at ?? null) }}</td>
              <td class="text-xs whitespace-nowrap text-muted">{{ formatDateTime(task.created_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <TablePager :model-value="taskPage + 1" :total="tasks.data.value?.total ?? 0" @update:model-value="taskPage = $event - 1" />
    </AsyncBlock>
  </section>

  <section v-else class="card" role="tabpanel">
    <div class="card-head flex flex-wrap items-center gap-2">
      <label class="text-xs text-muted" for="audit-action">Action</label>
      <select id="audit-action" v-model="action" class="input input-sm w-auto">
        <option value="">All</option>
        <option value="security.">Security events</option>
        <option value="approval.">Approvals</option>
        <option value="operation.">Executed operations</option>
        <option value="auth.">Sign-in and registration</option>
        <option value="repository.">Repositories</option>
        <option value="project.">Projects</option>
        <option value="task.">Tasks</option>
        <option value="github.">GitHub credentials</option>
      </select>
    </div>
    <AsyncBlock :loading="audit.loading.value" :error="audit.error.value" :empty="!audit.data.value?.items.length" empty-title="No audit entries match" @retry="audit.load">
      <div class="overflow-x-auto">
        <table class="table">
          <thead><tr><th>Time</th><th>Action</th><th>Actor</th><th>Resource</th><th>Details</th></tr></thead>
          <tbody>
            <tr v-for="entry in audit.data.value?.items ?? []" :key="entry.id">
              <td class="text-xs whitespace-nowrap text-muted">{{ formatDateTime(entry.created_at) }}</td>
              <td><code :class="{ 'text-bad': entry.action.startsWith('security.') }">{{ entry.action }}</code></td>
              <td class="text-xs">{{ entry.actor_type }}<span v-if="entry.ip_address" class="text-muted"> · {{ entry.ip_address }}</span></td>
              <td class="text-xs">{{ entry.resource_type ?? '—' }} <span class="font-mono text-muted">{{ entry.resource_id?.slice(0, 12) }}</span></td>
              <td class="max-w-md">
                <details v-if="Object.keys(entry.details).length">
                  <summary class="cursor-pointer text-xs text-muted">Show</summary>
                  <pre class="mt-1 max-h-48 overflow-auto text-xs whitespace-pre-wrap">{{ JSON.stringify(entry.details, null, 2) }}</pre>
                </details>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <TablePager :model-value="auditPage + 1" :total="audit.data.value?.total ?? 0" @update:model-value="auditPage = $event - 1" />
    </AsyncBlock>
  </section>
</template>
