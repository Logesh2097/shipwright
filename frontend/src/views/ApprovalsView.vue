<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'

import { api } from '@/api'
import type { Approval } from '@/api/types'
import ApprovalPanel from '@/components/ApprovalPanel.vue'
import AsyncBlock from '@/components/AsyncBlock.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { formatDateTime, titleCase } from '@/lib/format'
import { useAsync, usePolling } from '@/lib/useAsync'
import { useWorkspaceStore } from '@/stores/workspace'

const workspace = useWorkspaceStore()
const filter = ref<'pending' | 'decided'>('pending')

const approvals = useAsync(async () => {
  const result = await api.approvals({ status: filter.value === 'pending' ? 'pending' : undefined, limit: 100 })
  return filter.value === 'pending' ? result.items : result.items.filter((a) => a.status !== 'pending')
})
onMounted(approvals.load)
watch(filter, () => void approvals.load())
usePolling(approvals.refresh, 5000)

/** The exact operation, in words, for each kind of request. */
function describe(approval: Approval): { label: string; value: string }[] {
  const p = approval.payload
  const text = (value: unknown) => (Array.isArray(value) ? value.join(' ') : String(value ?? ''))
  switch (approval.action_type) {
    case 'apply_changes':
    case 'revert_changes':
      return [{ label: 'Files', value: (Array.isArray(p.files) ? p.files : []).map((f) => (typeof f === 'string' ? f : String((f as { path?: string }).path))).join(', ') }]
    case 'git_create_branch':
      return [{ label: 'New branch', value: text(p.name) }]
    case 'git_commit':
      return [
        { label: 'Files to stage and commit', value: (p.paths as string[]).join(', ') },
        { label: 'Commit message', value: text(p.message) },
      ]
    case 'git_push':
      return [
        { label: 'Branch', value: text(p.branch) },
        { label: 'Remote', value: text(p.repository) },
      ]
    case 'github_create_pr':
      return [
        { label: 'Repository', value: text(p.full_name) },
        { label: 'From → into', value: `${text(p.head)} → ${text(p.base)}` },
        { label: 'Title', value: text(p.title) + (p.draft ? ' (draft)' : '') },
        { label: 'Description', value: text(p.body) || '(empty)' },
      ]
    default:
      return [
        { label: 'Command', value: text(p.argv) },
        { label: 'Working directory', value: text(p.directory) || '.' },
      ]
  }
}
</script>

<template>
  <PageHeader
    title="Approvals"
    subtitle="Code changes, file deletions, dependency installs, migrations, commits, pushes and pull requests all wait here for an explicit decision."
  >
    <div class="seg" role="tablist">
      <button
        v-for="option in (['pending', 'decided'] as const)"
        :key="option"
        type="button"
        role="tab"
        class="seg__btn"
        :aria-selected="filter === option"
        @click="filter = option"
      >
        {{ option === 'pending' ? 'Waiting' : 'History' }}
      </button>
    </div>
  </PageHeader>

  <AsyncBlock
    :loading="approvals.loading.value"
    :error="approvals.error.value"
    :empty="!approvals.data.value?.length"
    :empty-title="filter === 'pending' ? 'Nothing is waiting for a decision' : 'No decisions recorded yet'"
    :empty-text="filter === 'pending' ? 'Requests appear here when the agent proposes changes or when someone asks for a sensitive operation.' : undefined"
    @retry="approvals.load"
  >
    <div class="space-y-4">
      <article v-for="approval in approvals.data.value ?? []" :key="approval.id">
        <template v-if="approval.status === 'pending'">
          <div class="card mb-2 p-4">
            <p class="text-xs text-muted">{{ titleCase(approval.action_type) }} · {{ workspace.repositoryName(approval.repository_id) }}</p>
            <dl class="mt-2 space-y-1.5">
              <div v-for="row in describe(approval)" :key="row.label" class="grid gap-x-3 sm:grid-cols-[11rem_1fr]">
                <dt class="text-xs text-muted">{{ row.label }}</dt>
                <dd class="font-mono text-xs break-words whitespace-pre-wrap">{{ row.value }}</dd>
              </div>
            </dl>
            <div class="mt-3 flex flex-wrap gap-2">
              <RouterLink v-if="approval.change_set_id" :to="`/changes/${approval.change_set_id}`" class="btn btn-sm btn-primary">Review the diff</RouterLink>
              <RouterLink v-if="approval.task_id" :to="`/tasks/${approval.task_id}`" class="btn btn-sm">Open task</RouterLink>
            </div>
          </div>
          <ApprovalPanel :approval="approval" @decided="approvals.refresh" />
        </template>
        <div v-else class="card p-4">
          <div class="flex flex-wrap items-center gap-2">
            <h2 class="min-w-0 truncate">{{ approval.title }}</h2>
            <StatusBadge :status="approval.status" />
          </div>
          <p class="mt-1 text-xs text-muted">
            {{ titleCase(approval.action_type) }} · {{ workspace.repositoryName(approval.repository_id) }} · decided {{ formatDateTime(approval.decided_at) }}
            <template v-if="approval.comment"> · “{{ approval.comment }}”</template>
          </p>
          <dl class="mt-2 space-y-1">
            <div v-for="row in describe(approval)" :key="row.label" class="grid gap-x-3 sm:grid-cols-[11rem_1fr]">
              <dt class="text-xs text-muted">{{ row.label }}</dt>
              <dd class="font-mono text-xs break-words">{{ row.value }}</dd>
            </div>
          </dl>
          <details v-if="approval.result" class="mt-2">
            <summary class="cursor-pointer text-xs text-muted">Execution result</summary>
            <pre class="mt-1 max-h-64 overflow-auto rounded bg-raised p-2 text-xs whitespace-pre-wrap">{{ JSON.stringify(approval.result, null, 2) }}</pre>
          </details>
          <RouterLink v-if="approval.change_set_id" :to="`/changes/${approval.change_set_id}`" class="link mt-2 inline-block text-xs">View change set</RouterLink>
        </div>
      </article>
    </div>
  </AsyncBlock>
</template>
