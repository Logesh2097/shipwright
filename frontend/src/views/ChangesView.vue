<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'

import { api } from '@/api'
import AsyncBlock from '@/components/AsyncBlock.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import TablePager from '@/components/TablePager.vue'
import { formatRelative } from '@/lib/format'
import { usePaged } from '@/lib/paging'
import { useAsync, usePolling } from '@/lib/useAsync'
import { useWorkspaceStore } from '@/stores/workspace'

const workspace = useWorkspaceStore()
const status = ref('')
const scope = ref<'repository' | 'all'>('repository')

const changeSets = useAsync(() =>
  api.changeSets({
    status: status.value || undefined,
    repository_id: scope.value === 'repository' ? (workspace.repositoryId ?? undefined) : undefined,
    limit: 100,
  }),
)
onMounted(changeSets.load)
watch([status, scope, () => workspace.repositoryId], () => void changeSets.load())
usePolling(changeSets.refresh, 6000)
const paged = usePaged(() => changeSets.data.value?.items)
</script>

<template>
  <PageHeader title="Change review" subtitle="Every change the agent proposes is stored here as an exact diff. Nothing is written until a developer approves it.">
    <label class="flex items-center gap-1.5 text-xs text-muted">
      Scope
      <select v-model="scope" class="input input-sm">
        <option value="repository">Selected repository</option>
        <option value="all">All my projects</option>
      </select>
    </label>
    <label class="flex items-center gap-1.5 text-xs text-muted">
      Status
      <select v-model="status" class="input input-sm">
        <option value="">Any</option>
        <option v-for="option in ['proposed', 'approved', 'applied', 'rejected', 'stale', 'failed', 'reverted', 'partially_reverted']" :key="option" :value="option">
          {{ option.replace('_', ' ') }}
        </option>
      </select>
    </label>
  </PageHeader>

  <section class="card">
    <AsyncBlock
      :loading="changeSets.loading.value"
      :error="changeSets.error.value"
      :empty="!changeSets.data.value?.items.length"
      empty-title="No change sets"
      empty-text="When a task needs code changes, the agent's proposal appears here for review."
      @retry="changeSets.load"
    >
      <div class="overflow-x-auto">
        <table class="table">
          <thead><tr><th>Change set</th><th>Repository</th><th>Files</th><th>Status</th><th>Proposed</th></tr></thead>
          <tbody>
            <tr v-for="changeSet in paged.rows" :key="changeSet.id">
              <td>
                <RouterLink :to="`/changes/${changeSet.id}`" class="link font-medium">{{ changeSet.title || 'Untitled change' }}</RouterLink>
                <p class="max-w-xl truncate text-xs text-muted">{{ changeSet.summary }}</p>
              </td>
              <td>{{ workspace.repositoryName(changeSet.repository_id) }}</td>
              <td>{{ changeSet.file_count }}</td>
              <td><StatusBadge :status="changeSet.status" /></td>
              <td class="text-xs whitespace-nowrap text-muted">{{ formatRelative(changeSet.created_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <TablePager v-model="paged.page" :total="paged.total" />
    </AsyncBlock>
  </section>
</template>
