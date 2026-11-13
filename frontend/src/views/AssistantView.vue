<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api } from '@/api'
import { ApiError, errorMessage } from '@/api/client'
import AsyncBlock from '@/components/AsyncBlock.vue'
import NoticeBox from '@/components/NoticeBox.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { formatRelative, NODE_LABELS, titleCase } from '@/lib/format'
import { useAsync, usePolling } from '@/lib/useAsync'
import { useWorkspaceStore } from '@/stores/workspace'

const workspace = useWorkspaceStore()
const router = useRouter()

const request = ref('')
const busy = ref(false)
const error = ref<string | null>(null)
const duplicateTaskId = ref<string | null>(null)

const EXAMPLES = [
  'Explain how the payment module works.',
  'Find and fix the bug in the customer login API.',
  'Add a new API endpoint for customer registration.',
  'Identify unused functions and imports.',
  'Generate unit tests for the pricing module.',
  'Investigate the failing tests and suggest a fix.',
]

const system = useAsync(() => api.systemSettings())
const tasks = useAsync(() =>
  workspace.repositoryId ? api.tasks({ repository_id: workspace.repositoryId, limit: 20 }) : Promise.resolve({ items: [], total: 0 }),
)
onMounted(() => {
  void system.load()
  void tasks.load()
})
watch(() => workspace.repositoryId, () => void tasks.load())
usePolling(tasks.refresh, 4000)

const repository = computed(() => workspace.repository)
const blocked = computed(() => {
  if (!repository.value) return 'Select a repository first (top bar), or register one under Projects & repositories.'
  if (!workspace.canWrite) return 'You have read-only access to this project, so you cannot start tasks.'
  if (repository.value.status === 'error') return 'This repository could not be indexed. Re-index it under Projects & repositories.'
  return null
})

async function submit(): Promise<void> {
  if (!repository.value || blocked.value) return
  busy.value = true
  error.value = null
  duplicateTaskId.value = null
  try {
    const task = await api.createTask(repository.value.id, request.value)
    await router.push(`/tasks/${task.id}`)
  } catch (cause) {
    error.value = errorMessage(cause)
    if (cause instanceof ApiError && cause.code === 'duplicate_task' && typeof cause.details.task_id === 'string') {
      duplicateTaskId.value = cause.details.task_id
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <PageHeader
    title="Developer assistant"
    subtitle="Describe what you need in plain language. The agent explores the repository, plans, and proposes changes for your review. Nothing is written without your approval."
  />

  <NoticeBox v-if="system.data.value && !system.data.value.llm.configured" tone="warn" title="Language model not configured" class="mb-4">
    Tasks submitted now will fail with <code>llm_not_configured</code>. Set <code>LLM_PROVIDER</code>, its API key and <code>LLM_MODEL</code> on the server and restart it.
  </NoticeBox>

  <section class="card p-4">
    <form @submit.prevent="submit">
      <label class="label" for="request">
        Request for <strong class="text-fg">{{ repository?.name ?? 'no repository selected' }}</strong>
      </label>
      <textarea
        id="request"
        v-model="request"
        class="input min-h-28"
        rows="4"
        maxlength="8000"
        placeholder="For example: The order total is wrong when a 10% discount is applied. Find the cause and fix it."
        :disabled="!!blocked"
        required
      ></textarea>
      <div class="mt-2 flex flex-wrap gap-1.5">
        <button v-for="example in EXAMPLES" :key="example" type="button" class="btn btn-sm font-normal" :disabled="!!blocked" @click="request = example">
          {{ example }}
        </button>
      </div>
      <NoticeBox v-if="blocked" tone="neutral" class="mt-3">{{ blocked }}</NoticeBox>
      <NoticeBox v-if="error" tone="bad" class="mt-3">
        {{ error }}
        <RouterLink v-if="duplicateTaskId" :to="`/tasks/${duplicateTaskId}`" class="link ml-1">Open the existing task</RouterLink>
      </NoticeBox>
      <div class="mt-3 flex flex-wrap items-center gap-3">
        <button class="btn btn-primary" type="submit" :disabled="busy || !!blocked || request.trim().length < 3">
          {{ busy ? 'Starting…' : 'Start task' }}
        </button>
        <span class="text-xs text-muted">The agent can read code and run sandboxed checks on its own. Changes, installs, commits and pushes always wait for you.</span>
      </div>
    </form>
  </section>

  <section class="card mt-4">
    <h2 class="card-head">Recent tasks in this repository</h2>
    <AsyncBlock
      :loading="tasks.loading.value"
      :error="tasks.error.value"
      :empty="!tasks.data.value?.items.length"
      empty-title="No tasks for this repository yet"
      empty-text="Your requests and their progress will appear here."
      @retry="tasks.load"
    >
      <ul>
        <li v-for="task in tasks.data.value?.items ?? []" :key="task.id" class="border-b border-line last:border-b-0">
          <RouterLink :to="`/tasks/${task.id}`" class="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 hover:bg-raised">
            <span class="min-w-0 flex-1 basis-64">
              <span class="block truncate font-medium">{{ task.title }}</span>
              <span class="block truncate text-xs text-muted">
                {{ titleCase(task.task_type) }} · {{ formatRelative(task.created_at) }}
                <template v-if="task.status === 'running' && task.latest_run?.current_node">
                  · {{ NODE_LABELS[task.latest_run.current_node] ?? task.latest_run.current_node }}
                </template>
                <template v-else-if="task.result_summary"> · {{ task.result_summary }}</template>
              </span>
            </span>
            <StatusBadge :status="task.status" />
          </RouterLink>
        </li>
      </ul>
    </AsyncBlock>
  </section>
</template>
