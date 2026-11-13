<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api'
import { errorMessage } from '@/api/client'
import type { Approval, ChangeSet } from '@/api/types'
import ApprovalPanel from '@/components/ApprovalPanel.vue'
import AsyncBlock from '@/components/AsyncBlock.vue'
import DiffViewer from '@/components/DiffViewer.vue'
import MarkdownView from '@/components/MarkdownView.vue'
import NoticeBox from '@/components/NoticeBox.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { diffStats } from '@/lib/diff'
import { formatDateTime } from '@/lib/format'
import { usePolling } from '@/lib/useAsync'
import { useWorkspaceStore } from '@/stores/workspace'

const props = defineProps<{ id: string }>()
const workspace = useWorkspaceStore()

const changeSet = ref<ChangeSet | null>(null)
const approval = ref<Approval | null>(null)
const staleFiles = ref<{ path: string; reason: string }[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const reverting = ref(false)
const actionMessage = ref<{ tone: 'ok' | 'bad' | 'warn'; text: string } | null>(null)

const totals = computed(() => {
  let additions = 0
  let deletions = 0
  for (const file of changeSet.value?.files ?? []) {
    const stats = diffStats(file.diff)
    additions += stats.additions
    deletions += stats.deletions
  }
  return { additions, deletions }
})
const canRevert = computed(() => workspace.canWrite && ['applied', 'partially_reverted'].includes(changeSet.value?.status ?? ''))

async function load(showLoading = true): Promise<void> {
  if (showLoading) loading.value = true
  try {
    const loaded = await api.changeSet(props.id)
    changeSet.value = loaded
    const [approvals, staleness] = await Promise.all([
      loaded.task_id ? api.approvals({ task_id: loaded.task_id, limit: 100 }) : api.approvals({ repository_id: loaded.repository_id, limit: 100 }),
      api.staleness(props.id),
    ])
    approval.value = approvals.items.find((a) => a.change_set_id === loaded.id && a.action_type === 'apply_changes') ?? null
    staleFiles.value = staleness.applicable ? staleness.stale_files : []
    error.value = null
  } catch (cause) {
    if (showLoading || !changeSet.value) error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

onMounted(() => load())
// Keeps the staleness warning and status current while the page is open.
usePolling(() => load(false), 5000)

async function revert(): Promise<void> {
  if (!changeSet.value) return
  if (!window.confirm('Restore the files to what they were before this change set? Files edited since the agent wrote them are left untouched.')) return
  reverting.value = true
  actionMessage.value = null
  try {
    const result = await api.revertChangeSet(changeSet.value.id)
    changeSet.value = result
    const conflicts = result.files.filter((f) => f.status === 'revert_conflict')
    actionMessage.value = conflicts.length
      ? { tone: 'warn', text: `${conflicts.length} file(s) were changed after the agent wrote them and were left untouched: ${conflicts.map((f) => f.path).join(', ')}` }
      : { tone: 'ok', text: 'The files were restored.' }
  } catch (cause) {
    actionMessage.value = { tone: 'bad', text: errorMessage(cause) }
  } finally {
    reverting.value = false
  }
}
</script>

<template>
  <AsyncBlock :loading="loading" :error="error" @retry="load()">
    <template v-if="changeSet">
      <PageHeader
        :title="changeSet.title || 'Change set'"
        :subtitle="`${workspace.repositoryName(changeSet.repository_id)} · proposed ${formatDateTime(changeSet.created_at)}`"
      >
        <StatusBadge :status="changeSet.status" />
        <RouterLink v-if="changeSet.task_id" :to="`/tasks/${changeSet.task_id}`" class="btn btn-sm">Open task</RouterLink>
        <button v-if="canRevert" class="btn btn-sm btn-danger" type="button" :disabled="reverting" @click="revert">
          {{ reverting ? 'Reverting…' : 'Revert these changes' }}
        </button>
      </PageHeader>

      <div class="space-y-3">
        <NoticeBox v-if="actionMessage" :tone="actionMessage.tone">{{ actionMessage.text }}</NoticeBox>
        <NoticeBox v-if="changeSet.status === 'stale'" tone="bad" title="Not applied: the files changed underneath">
          One or more files were modified after this proposal was created, so nothing was written. Start a new task to get a proposal based on the current code.
        </NoticeBox>
        <NoticeBox v-else-if="changeSet.status === 'failed'" tone="bad" title="Applying this change set failed">
          Files that had already been written were restored. See the per-file messages below.
        </NoticeBox>
        <NoticeBox v-else-if="changeSet.status === 'applied'" tone="ok">
          Applied {{ formatDateTime(changeSet.applied_at) }}. The changes are in your working tree, unstaged and uncommitted.
        </NoticeBox>
        <ApprovalPanel v-if="approval" :approval="approval" :stale-files="staleFiles" @decided="load(false)" />
      </div>

      <section v-if="changeSet.summary" class="card mt-4 p-4">
        <h2>Agent's summary</h2>
        <MarkdownView :source="changeSet.summary" class="mt-1" />
      </section>

      <section class="mt-4">
        <div class="mb-2 flex flex-wrap items-center gap-3">
          <h2>{{ changeSet.files.length }} file(s) changed</h2>
          <span class="font-mono text-xs"><span class="text-ok">+{{ totals.additions }}</span> <span class="text-bad">−{{ totals.deletions }}</span></span>
          <span v-if="changeSet.base_commit" class="text-xs text-muted">based on commit {{ changeSet.base_commit.slice(0, 10) }}</span>
        </div>
        <nav v-if="changeSet.files.length > 1" class="card mb-3 p-3" aria-label="Files in this change set">
          <ul class="space-y-1 font-mono text-xs">
            <li v-for="file in changeSet.files" :key="file.id" class="flex flex-wrap items-center gap-2">
              <a :href="`#file-${file.id}`" class="link">{{ file.path }}</a>
              <span class="text-muted">{{ file.change_type }}</span>
              <StatusBadge :status="file.status" />
            </li>
          </ul>
        </nav>
        <div class="space-y-4">
          <div v-for="file in changeSet.files" :id="`file-${file.id}`" :key="file.id" class="scroll-mt-16">
            <div v-if="changeSet.files.length === 1 || file.error" class="mb-1 flex flex-wrap items-center gap-2">
              <StatusBadge :status="file.status" />
              <span v-if="file.error" class="text-xs text-bad">{{ file.error }}</span>
            </div>
            <DiffViewer :diff="file.diff" :path="file.path" :change-type="file.change_type" />
          </div>
        </div>
      </section>
    </template>
  </AsyncBlock>
</template>
