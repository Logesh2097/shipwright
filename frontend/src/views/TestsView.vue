<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { api } from '@/api'
import { errorMessage } from '@/api/client'
import type { TestRunDetail } from '@/api/types'
import AsyncBlock from '@/components/AsyncBlock.vue'
import NoticeBox from '@/components/NoticeBox.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import TestCounts from '@/components/TestCounts.vue'
import { formatDateTime, formatDuration } from '@/lib/format'
import { useAsync } from '@/lib/useAsync'
import { useWorkspaceStore } from '@/stores/workspace'

const workspace = useWorkspaceStore()
const route = useRoute()
const repoId = () => workspace.repositoryId ?? ''

const discovery = useAsync(() => api.discoverTests(repoId()))
const runs = useAsync(() => (workspace.repositoryId ? api.testRuns({ repository_id: workspace.repositoryId, limit: 50 }) : Promise.resolve({ items: [], total: 0 })))
const selected = ref<TestRunDetail | null>(null)
const selectedError = ref<string | null>(null)
const running = ref<string | null>(null)
const runError = ref<string | null>(null)
const targets = ref('')
const outcomeFilter = ref('')

interface Check {
  key: string
  label: string
  kind: string
  tool: string
  directory: string
  acceptsTargets: boolean
}

const checks = computed<Check[]>(() => {
  const data = discovery.data.value
  if (!data) return []
  const list: Check[] = []
  for (const framework of data.test_frameworks) {
    list.push({
      key: `test-${framework.framework}-${framework.directory}`,
      label: `Run ${framework.framework} tests`,
      kind: 'unit',
      tool: framework.framework,
      directory: framework.directory,
      acceptsTargets: framework.framework !== 'npm',
    })
  }
  for (const tool of data.quality_tools) {
    list.push({
      key: `${tool.kind}-${tool.tool}-${tool.directory}`,
      label: tool.kind === 'lint' ? `Lint with ${tool.tool}` : tool.kind === 'typecheck' ? `Type-check with ${tool.tool}` : 'Build (npm run build)',
      kind: tool.kind,
      tool: tool.tool,
      directory: tool.directory,
      acceptsTargets: false,
    })
  }
  return list
})

async function open(id: string): Promise<void> {
  selectedError.value = null
  outcomeFilter.value = ''
  try {
    selected.value = await api.testRun(id)
  } catch (cause) {
    selected.value = null
    selectedError.value = errorMessage(cause)
  }
}

async function run(check: Check): Promise<void> {
  running.value = check.key
  runError.value = null
  try {
    const list = check.acceptsTargets ? targets.value.split(/[\s,]+/).filter(Boolean) : []
    selected.value = await api.runCheck(repoId(), { kind: check.kind, tool: check.tool, targets: list }, check.directory)
    outcomeFilter.value = ''
    await runs.refresh()
  } catch (cause) {
    runError.value = errorMessage(cause)
  } finally {
    running.value = null
  }
}

function load(): void {
  selected.value = null
  if (!workspace.repositoryId) return
  void discovery.load()
  void runs.load()
}

const visibleCases = computed(() => (selected.value?.cases ?? []).filter((c) => !outcomeFilter.value || c.outcome === outcomeFilter.value))

onMounted(() => {
  load()
  if (typeof route.query.run === 'string') void open(route.query.run)
})
watch(() => workspace.repositoryId, load)
</script>

<template>
  <PageHeader
    title="Tests & checks"
    subtitle="Tests, linters, type checkers and builds run in the sandbox. Passed, failed, skipped, errored and not-executed results are always reported separately."
  />

  <AsyncBlock :loading="false" :error="null" :empty="!workspace.repository" empty-title="No repository selected" empty-text="Choose a repository in the top bar.">
    <section class="card">
      <h2 class="card-head">Available checks for {{ workspace.repository?.name }}</h2>
      <AsyncBlock
        :loading="discovery.loading.value"
        :error="discovery.error.value"
        :empty="!checks.length"
        empty-title="No test framework or quality tool detected"
        empty-text="Detection looks for pytest, Vitest, Jest, npm test scripts, Ruff, ESLint, mypy, tsc and vue-tsc in the repository's manifests."
        @retry="discovery.load"
      >
        <div class="p-4">
          <p class="text-xs text-muted">{{ discovery.data.value?.test_file_count }} test file(s) found.</p>
          <div v-if="workspace.canWrite" class="mt-3">
            <label class="label" for="targets">Limit test runs to these files or directories (optional, space separated, relative to the tool's directory)</label>
            <input id="targets" v-model="targets" class="input font-mono" placeholder="tests/test_pricing.py" />
          </div>
          <ul class="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            <li v-for="check in checks" :key="check.key" class="flex items-center justify-between gap-2 rounded-md border border-line p-3">
              <span class="min-w-0">
                <span class="block truncate font-medium">{{ check.label }}</span>
                <span class="block truncate text-xs text-muted">in {{ check.directory === '.' ? 'repository root' : check.directory }}</span>
              </span>
              <button v-if="workspace.canWrite" class="btn btn-sm" type="button" :disabled="running !== null" @click="run(check)">
                {{ running === check.key ? 'Running…' : 'Run' }}
              </button>
            </li>
          </ul>
          <NoticeBox v-if="!workspace.canWrite" tone="neutral" class="mt-3">You have read-only access, so you can view results but not start runs.</NoticeBox>
          <NoticeBox v-if="running" tone="info" class="mt-3">The check is running in the sandbox. Large suites can take several minutes.</NoticeBox>
          <NoticeBox v-if="runError" tone="bad" class="mt-3">{{ runError }}</NoticeBox>
          <p class="mt-3 text-xs text-muted">
            Builds write their output (for example <code>dist/</code>) into the working tree. Tests, linters and type checkers run against a read-only mount when the Docker
            sandbox is active.
          </p>
        </div>
      </AsyncBlock>
    </section>

    <div class="mt-4 grid gap-4 lg:grid-cols-5">
      <section class="card lg:col-span-2">
        <h2 class="card-head">Runs</h2>
        <AsyncBlock
          :loading="runs.loading.value"
          :error="runs.error.value"
          :empty="!runs.data.value?.items.length"
          empty-title="No runs recorded"
          empty-text="Runs started here or by the agent during verification are listed with their results."
          @retry="runs.load"
        >
          <ul class="max-h-[40rem] overflow-y-auto">
            <li v-for="item in runs.data.value?.items ?? []" :key="item.id" class="border-b border-line last:border-b-0">
              <button
                type="button"
                class="block w-full px-4 py-2.5 text-left hover:bg-raised"
                :class="{ 'bg-accent-soft': selected?.id === item.id }"
                @click="open(item.id)"
              >
                <span class="flex items-center justify-between gap-2">
                  <span class="font-medium">{{ item.framework }} · {{ item.kind }}</span>
                  <StatusBadge :status="item.status" />
                </span>
                <span class="block text-xs text-muted">
                  {{ formatDateTime(item.created_at) }} · {{ formatDuration(item.duration_ms) }} · {{ item.run_id ? 'by the agent' : 'manual' }}
                </span>
                <TestCounts class="mt-1" :passed="item.passed" :failed="item.failed" :skipped="item.skipped" :errors="item.errors" :not-executed="item.not_executed" />
              </button>
            </li>
          </ul>
        </AsyncBlock>
      </section>

      <section class="card min-w-0 lg:col-span-3">
        <h2 class="card-head">Result</h2>
        <AsyncBlock :loading="false" :error="selectedError" :empty="!selected" empty-title="Select a run" empty-text="Per-test outcomes and the captured output are shown here.">
          <div v-if="selected" class="p-4">
            <div class="flex flex-wrap items-center gap-2">
              <h3>{{ selected.framework }} · {{ selected.kind }}</h3>
              <StatusBadge :status="selected.status" />
              <span class="text-xs text-muted">{{ formatDuration(selected.duration_ms) }}</span>
            </div>
            <TestCounts class="mt-2" :passed="selected.passed" :failed="selected.failed" :skipped="selected.skipped" :errors="selected.errors" :not-executed="selected.not_executed" />
            <NoticeBox v-if="selected.status === 'not_executed'" tone="warn" title="This check did not run" class="mt-3">
              {{ selected.output_excerpt.split('\n')[0] || 'No tests were executed.' }}
            </NoticeBox>
            <p v-if="selected.command.length" class="mt-3 text-xs text-muted">Command</p>
            <code v-if="selected.command.length" class="block overflow-x-auto rounded bg-raised p-2 whitespace-pre">{{ selected.command.join(' ') }}</code>

            <template v-if="selected.cases.length">
              <div class="mt-4 flex flex-wrap items-center gap-2">
                <h3>{{ selected.kind === 'lint' || selected.kind === 'typecheck' ? 'Issues' : 'Tests' }} ({{ visibleCases.length }})</h3>
                <select v-model="outcomeFilter" class="input input-sm w-auto" aria-label="Filter by outcome">
                  <option value="">All outcomes</option>
                  <option v-for="outcome in ['failed', 'error', 'not_executed', 'skipped', 'passed']" :key="outcome" :value="outcome">{{ outcome.replace('_', ' ') }}</option>
                </select>
              </div>
              <ul class="mt-2 max-h-[28rem] divide-y divide-line overflow-y-auto rounded-md border border-line">
                <li v-for="testCase in visibleCases" :key="testCase.id" class="px-3 py-2">
                  <div class="flex items-start justify-between gap-2">
                    <code class="break-all">{{ testCase.node_id }}</code>
                    <StatusBadge :status="testCase.outcome" />
                  </div>
                  <pre v-if="testCase.message" class="mt-1 max-h-48 overflow-auto rounded bg-raised p-2 text-xs whitespace-pre-wrap">{{ testCase.message }}</pre>
                </li>
              </ul>
            </template>

            <details v-if="selected.output_excerpt" class="mt-4" :open="!selected.cases.length">
              <summary class="cursor-pointer font-medium">Captured output</summary>
              <pre class="mt-2 max-h-96 overflow-auto rounded bg-raised p-3 text-xs whitespace-pre-wrap">{{ selected.output_excerpt }}</pre>
            </details>
          </div>
        </AsyncBlock>
      </section>
    </div>
  </AsyncBlock>
</template>
