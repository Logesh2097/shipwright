<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { api } from '@/api'
import type { ToolExecution } from '@/api/types'
import AsyncBlock from '@/components/AsyncBlock.vue'
import NoticeBox from '@/components/NoticeBox.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import TablePager from '@/components/TablePager.vue'
import { formatDateTime, formatDuration, shortId } from '@/lib/format'
import { PAGE_SIZE, usePaged } from '@/lib/paging'
import { useAsync, usePolling } from '@/lib/useAsync'
import { useWorkspaceStore } from '@/stores/workspace'

const workspace = useWorkspaceStore()
const servers = useAsync(() => api.mcpServers())
const tools = useAsync(() => api.mcpTools())
const pagedSessions = usePaged(() => servers.data.value?.sessions)

const statusFilter = ref('')
const scope = ref<'repository' | 'all'>('all')
const page = ref(0)
const executions = useAsync(() =>
  api.toolExecutions({
    status: statusFilter.value || undefined,
    repository_id: scope.value === 'repository' ? (workspace.repositoryId ?? undefined) : undefined,
    limit: PAGE_SIZE,
    offset: page.value * PAGE_SIZE,
  }),
)
const expanded = ref<string | null>(null)

onMounted(() => {
  void servers.load()
  void tools.load()
  void executions.load()
})
watch([statusFilter, scope, () => workspace.repositoryId], () => {
  page.value = 0
  void executions.load()
})
watch(page, () => void executions.load())
usePolling(() => {
  void servers.refresh()
  if (page.value === 0) void executions.refresh()
}, 5000)

const toolGroups = computed(() => {
  const groups: Record<string, NonNullable<typeof tools.data.value>['tools']> = {}
  for (const tool of tools.data.value?.tools ?? []) (groups[tool.server] ??= []).push(tool)
  return groups
})

function toggle(execution: ToolExecution): void {
  expanded.value = expanded.value === execution.id ? null : execution.id
}
</script>

<template>
  <PageHeader
    title="MCP & tool activity"
    subtitle="The agent reaches the repository only through these tools. Every call, including denied ones, is recorded here."
  />

  <div class="grid gap-4">
    <section class="card">
      <h2 class="card-head">MCP servers</h2>
      <AsyncBlock :loading="servers.loading.value" :error="servers.error.value" @retry="servers.load">
        <div v-if="servers.data.value" class="p-4">
          <ul class="space-y-2">
            <li v-for="definition in servers.data.value.definitions" :key="definition.kind" class="rounded-md border border-line p-3">
              <p class="font-medium">{{ definition.kind }} <span class="font-normal text-muted">· {{ definition.transport }}</span></p>
              <p class="text-xs text-muted">Toolsets: {{ definition.toolsets.join(', ') }} · {{ definition.scope }}</p>
            </li>
          </ul>
          <h3 class="mt-4">Connected sessions</h3>
          <p v-if="!servers.data.value.sessions.length" class="mt-1 text-muted">
            None right now. A server process starts when a repository is first used and stops after 15 minutes of inactivity.
          </p>
          <div v-else class="mt-1 overflow-x-auto">
            <table class="table">
              <thead><tr><th>Server</th><th>Repository</th><th>State</th><th>Calls</th><th>Errors</th><th>Idle</th></tr></thead>
              <tbody>
                <tr v-for="session in pagedSessions.rows" :key="session.kind + session.repository_id + session.user_id">
                  <td>{{ session.kind }} <span class="text-xs text-muted">({{ session.transport }}, {{ session.tool_count }} tools)</span></td>
                  <td>{{ session.repository_name ?? '—' }}</td>
                  <td><StatusBadge :status="session.alive ? 'success' : 'failed'" :label="session.alive ? 'Connected' : 'Disconnected'" /></td>
                  <td>{{ session.calls }}</td>
                  <td>{{ session.errors }}</td>
                  <td class="text-xs text-muted">{{ session.idle_seconds }} s</td>
                </tr>
              </tbody>
            </table>
          </div>
          <TablePager v-model="pagedSessions.page" :total="pagedSessions.total" />
          <p class="mt-3 text-xs text-muted">
            Restarts after a lost connection: {{ servers.data.value.restarts }} · Sandbox: {{ servers.data.value.sandbox.mode }} ({{ servers.data.value.sandbox.detail }})
          </p>
        </div>
      </AsyncBlock>
    </section>

    <section class="card">
      <h2 class="card-head">Tools and permissions</h2>
      <AsyncBlock :loading="tools.loading.value" :error="tools.error.value" @retry="tools.load">
        <div v-if="tools.data.value" class="max-h-[30rem] overflow-y-auto p-4">
          <dl class="space-y-1.5 text-xs">
            <div v-for="(description, level) in tools.data.value.risk_levels" :key="level" class="flex gap-2">
              <dt class="w-20 shrink-0"><StatusBadge :status="String(level)" /></dt>
              <dd class="text-muted">{{ description }}</dd>
            </div>
          </dl>
          <div v-for="(group, server) in toolGroups" :key="server" class="mt-4">
            <h3>{{ server }} <span class="font-normal text-muted">({{ group.length }})</span></h3>
            <ul class="mt-1 divide-y divide-line rounded-md border border-line">
              <li v-for="tool in group" :key="tool.name" class="px-3 py-1.5">
                <details>
                  <summary class="flex cursor-pointer items-center justify-between gap-2">
                    <code>{{ tool.name }}</code>
                    <span class="flex items-center gap-1.5">
                      <span v-if="tool.requires_approval" class="text-xs text-warn">needs approval</span>
                      <StatusBadge :status="tool.risk" />
                    </span>
                  </summary>
                  <p class="mt-1 text-xs text-muted">{{ tool.description ?? 'Description is shown once a server of this kind has been started.' }}</p>
                  <p v-if="tool.input_schema?.properties" class="mt-1 font-mono text-xs">
                    Arguments: {{ Object.keys(tool.input_schema.properties).join(', ') || 'none' }}
                  </p>
                </details>
              </li>
            </ul>
          </div>
        </div>
      </AsyncBlock>
    </section>
  </div>

  <section class="card mt-4">
    <div class="card-head card-head--plain flex flex-wrap items-center justify-between gap-2">
      <h2>Tool execution log</h2>
      <div class="flex flex-wrap items-center gap-2">
        <select v-model="scope" class="input input-sm w-auto" aria-label="Scope">
          <option value="all">All my projects</option>
          <option value="repository">Selected repository</option>
        </select>
        <select v-model="statusFilter" class="input input-sm w-auto" aria-label="Status">
          <option value="">Any status</option>
          <option v-for="status in ['success', 'error', 'denied', 'timeout']" :key="status" :value="status">{{ status }}</option>
        </select>
      </div>
    </div>
    <AsyncBlock
      :loading="executions.loading.value"
      :error="executions.error.value"
      :empty="!executions.data.value?.items.length"
      empty-title="No tool calls recorded"
      empty-text="Calls made by the agent, by the explorer screens and by approved operations are logged here."
      @retry="executions.load"
    >
      <NoticeBox v-if="statusFilter === 'denied'" tone="neutral" class="m-3">
        Denied calls are attempts that policy blocked: unknown tools, tools not offered in that step, missing approvals, or insufficient role.
      </NoticeBox>
      <div class="overflow-x-auto">
        <table class="table">
          <thead><tr><th>Time</th><th>Tool</th><th>Risk</th><th>Status</th><th>Duration</th><th>Run</th></tr></thead>
          <tbody>
            <template v-for="execution in executions.data.value?.items ?? []" :key="execution.id">
              <tr class="cursor-pointer hover:bg-raised" @click="toggle(execution)">
                <td class="text-xs whitespace-nowrap text-muted">{{ formatDateTime(execution.created_at) }}</td>
                <td>
                  <button type="button" class="font-mono text-left" :aria-expanded="expanded === execution.id" @click.stop="toggle(execution)">{{ execution.tool_name }}</button>
                  <span class="ml-1 text-xs text-muted">{{ execution.server }}</span>
                </td>
                <td><StatusBadge :status="execution.risk_level" /></td>
                <td><StatusBadge :status="execution.status" /></td>
                <td class="text-xs">{{ formatDuration(execution.duration_ms) }}</td>
                <td class="font-mono text-xs">{{ execution.run_id ? shortId(execution.run_id) : 'manual' }}</td>
              </tr>
              <tr v-if="expanded === execution.id">
                <td colspan="6" class="bg-raised">
                  <p class="text-xs text-muted">Arguments (secrets redacted, long values shortened)</p>
                  <pre class="max-h-48 overflow-auto text-xs whitespace-pre-wrap">{{ JSON.stringify(execution.arguments, null, 2) }}</pre>
                  <p v-if="execution.error" class="mt-2 text-xs text-bad">{{ execution.error }}</p>
                  <template v-if="execution.result_preview">
                    <p class="mt-2 text-xs text-muted">Result preview</p>
                    <pre class="max-h-64 overflow-auto text-xs whitespace-pre-wrap">{{ execution.result_preview }}</pre>
                  </template>
                  <p v-if="execution.approval_id" class="mt-2 text-xs text-muted">Authorised by approval {{ shortId(execution.approval_id) }}</p>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
      <TablePager :model-value="page + 1" :total="executions.data.value?.total ?? 0" @update:model-value="page = $event - 1" />
    </AsyncBlock>
  </section>
</template>
