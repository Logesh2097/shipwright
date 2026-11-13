<script setup lang="ts">
import { computed, onMounted } from 'vue'

import { api } from '@/api'
import AsyncBlock from '@/components/AsyncBlock.vue'
import NoticeBox from '@/components/NoticeBox.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { formatNumber, formatRelative, titleCase } from '@/lib/format'
import { useAsync, usePolling } from '@/lib/useAsync'

// Every number on this page is a count returned by the API for what the signed-in user can see.
const summary = useAsync(() => api.dashboard())
onMounted(summary.load)
usePolling(summary.refresh, 15_000)

const total = (counts: Record<string, number> | undefined) => Object.values(counts ?? {}).reduce((a, b) => a + b, 0)
const taskTotal = computed(() => total(summary.data.value?.tasks_by_status))
const activeTasks = computed(() => {
  const counts = summary.data.value?.tasks_by_status ?? {}
  return (counts.running ?? 0) + (counts.queued ?? 0) + (counts.awaiting_approval ?? 0)
})

function entries(counts: Record<string, number> | undefined): [string, number][] {
  return Object.entries(counts ?? {})
    .filter(([key]) => key !== 'null')
    .sort((a, b) => b[1] - a[1])
}
</script>

<template>
  <PageHeader title="Dashboard" subtitle="Current state of your projects, agent tasks and checks.">
    <RouterLink class="btn btn-primary" to="/assistant"><span class="mdi mdi-plus text-lg" aria-hidden="true"></span>New task</RouterLink>
  </PageHeader>

  <AsyncBlock :loading="summary.loading.value" :error="summary.error.value" @retry="summary.load">
    <template v-if="summary.data.value">
      <div class="mb-4 space-y-2">
        <NoticeBox v-if="!summary.data.value.system.llm_configured" tone="warn" title="Language model not configured">
          Agent tasks will fail until an API key (<code>OPENAI_API_KEY</code> or <code>GEMINI_API_KEY</code>) and <code>LLM_MODEL</code> are set on the server. Repository exploration, checks, Git and
          approvals work without it.
        </NoticeBox>
        <NoticeBox v-if="summary.data.value.system.sandbox.mode === 'local'" tone="warn" title="Commands run without container isolation">
          {{ summary.data.value.system.sandbox.detail }}. Only register repositories you trust.
        </NoticeBox>
        <NoticeBox v-else-if="summary.data.value.system.sandbox.mode === 'unavailable'" tone="bad" title="Sandbox unavailable">
          {{ summary.data.value.system.sandbox.detail }}. Tests and commands cannot run.
        </NoticeBox>
      </div>

      <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <RouterLink to="/projects" class="card tile">
          <p class="tile__head"><span class="tile__icon mdi mdi-folder-multiple-outline" aria-hidden="true"></span>Projects · repositories</p>
          <p class="tile__value">{{ summary.data.value.projects }} · {{ summary.data.value.repositories }}</p>
        </RouterLink>
        <RouterLink to="/history" class="card tile">
          <p class="tile__head"><span class="tile__icon mdi mdi-clipboard-text-clock-outline" aria-hidden="true"></span>Tasks (active)</p>
          <p class="tile__value">{{ taskTotal }} <span class="text-base font-normal text-muted">({{ activeTasks }})</span></p>
        </RouterLink>
        <RouterLink to="/approvals" class="card tile" :class="{ 'border-warn': summary.data.value.pending_approvals > 0 }">
          <p class="tile__head"><span class="tile__icon mdi mdi-check-decagram-outline" aria-hidden="true"></span>Waiting for your decision</p>
          <p class="tile__value" :class="{ 'text-warn': summary.data.value.pending_approvals > 0 }">
            {{ summary.data.value.pending_approvals }}
          </p>
        </RouterLink>
        <div class="card tile">
          <p class="tile__head"><span class="tile__icon mdi mdi-creation-outline" aria-hidden="true"></span>Model usage (all runs)</p>
          <p class="tile__value">{{ formatNumber(summary.data.value.llm_usage.calls) }} <span class="text-base font-normal text-muted">calls</span></p>
          <p class="text-xs text-muted">
            {{ formatNumber(summary.data.value.llm_usage.input_tokens) }} in · {{ formatNumber(summary.data.value.llm_usage.output_tokens) }} out tokens
          </p>
        </div>
      </div>

      <div class="mt-4 grid gap-3 lg:grid-cols-3">
        <section class="card min-w-0 lg:col-span-2">
          <h2 class="card-head">Recent tasks</h2>
          <AsyncBlock
            :loading="false"
            :error="null"
            :empty="!summary.data.value.recent_tasks.length"
            empty-title="No tasks yet"
            empty-text="Register a repository, then ask the assistant to explain, fix or extend something."
          >
            <template #emptyAction><RouterLink class="btn" to="/projects">Register a repository</RouterLink></template>
            <ul>
              <li v-for="task in summary.data.value.recent_tasks" :key="task.id" class="border-b border-line last:border-b-0">
                <RouterLink :to="`/tasks/${task.id}`" class="flex items-center gap-3 px-4 py-2.5 hover:bg-raised">
                  <span class="min-w-0 flex-1 truncate">{{ task.title }}</span>
                  <span class="hidden text-xs text-muted sm:inline">{{ titleCase(task.task_type) }}</span>
                  <span class="hidden text-xs text-muted sm:inline">{{ formatRelative(task.created_at) }}</span>
                  <StatusBadge :status="task.status" />
                </RouterLink>
              </li>
            </ul>
          </AsyncBlock>
        </section>

        <section class="card">
          <h2 class="card-head">Breakdown</h2>
          <dl class="space-y-3 p-4 text-sm">
            <div v-for="group in [
              { title: 'Tasks', counts: summary.data.value.tasks_by_status },
              { title: 'Change sets', counts: summary.data.value.change_sets_by_status },
              { title: 'Test and check runs', counts: summary.data.value.test_runs_by_status },
              { title: 'Tool calls', counts: summary.data.value.tool_calls_by_status },
            ]" :key="group.title">
              <dt class="text-xs font-medium text-muted">{{ group.title }}</dt>
              <dd class="mt-1 flex flex-wrap gap-1.5">
                <span v-if="!entries(group.counts).length" class="text-muted">None recorded</span>
                <StatusBadge v-for="[status, count] in entries(group.counts)" :key="status" :status="status" :label="`${titleCase(status)} ${count}`" />
              </dd>
            </div>
          </dl>
          <p class="border-t border-line px-4 py-3 text-xs text-muted">
            {{ summary.data.value.system.active_runs }} run(s) executing · {{ summary.data.value.system.mcp_sessions }} MCP server(s) connected · sandbox:
            {{ summary.data.value.system.sandbox.mode }}
          </p>
        </section>
      </div>
    </template>
  </AsyncBlock>
</template>
