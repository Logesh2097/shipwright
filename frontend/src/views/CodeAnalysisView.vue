<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'

import { api } from '@/api'
import { errorMessage } from '@/api/client'
import type { FileContent, RouteHit, SymbolHit, TreeEntry, UnusedCode } from '@/api/types'
import AsyncBlock from '@/components/AsyncBlock.vue'
import NoticeBox from '@/components/NoticeBox.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import TablePager from '@/components/TablePager.vue'
import { usePaged } from '@/lib/paging'
import { useAsync } from '@/lib/useAsync'
import { useWorkspaceStore } from '@/stores/workspace'

const workspace = useWorkspaceStore()
type Tab = 'overview' | 'files' | 'search' | 'routes' | 'unused'
const tab = ref<Tab>('overview')
const TABS: { key: Tab; label: string }[] = [
  { key: 'overview', label: 'Overview' },
  { key: 'files', label: 'Files' },
  { key: 'search', label: 'Search' },
  { key: 'routes', label: 'APIs & pages' },
  { key: 'unused', label: 'Unused code' },
]

const repoId = () => workspace.repositoryId ?? ''
const context = useAsync(() => api.context(repoId()))
const routes = useAsync<{ items: RouteHit[] }>(() => api.routes(repoId()))
const unused = useAsync<UnusedCode>(() => api.unusedCode(repoId()))
const pagedRoutes = usePaged(() => routes.data.value?.items)
const pagedImports = usePaged(() => unused.data.value?.unused_imports)
const pagedDefinitions = usePaged(() => unused.data.value?.unused_definitions)

// --- file browser
const treePath = ref('.')
const tree = useAsync<{ path: string; entries: TreeEntry[]; truncated: boolean; hidden_entries: number }>(() => api.tree(repoId(), treePath.value))
const file = ref<FileContent | null>(null)
const fileError = ref<string | null>(null)
const fileLoading = ref(false)

async function openDirectory(path: string): Promise<void> {
  treePath.value = path
  await tree.load()
}

async function openFile(path: string, line = 1): Promise<void> {
  tab.value = 'files'
  fileLoading.value = true
  fileError.value = null
  try {
    file.value = await api.file(repoId(), path, Math.max(1, line - 20))
  } catch (cause) {
    file.value = null
    fileError.value = errorMessage(cause)
  } finally {
    fileLoading.value = false
  }
}

function parentOf(path: string): string {
  const parts = path.split('/')
  parts.pop()
  return parts.length ? parts.join('/') : '.'
}

// --- search
const query = ref('')
const searchMode = ref<'symbols' | 'text' | 'relevant'>('symbols')
const searching = ref(false)
const searchError = ref<string | null>(null)
const searched = ref(false)
const symbolHits = ref<SymbolHit[]>([])
const textHits = ref<{ path: string; line: number; text: string }[]>([])
const relevantHits = ref<{ path: string; score: number; reasons: string[] }[]>([])
const pagedSymbols = usePaged(() => symbolHits.value)
const pagedText = usePaged(() => textHits.value)
const pagedRelevant = usePaged(() => relevantHits.value)

async function search(): Promise<void> {
  if (!query.value.trim()) return
  searching.value = true
  searchError.value = null
  try {
    if (searchMode.value === 'symbols') symbolHits.value = (await api.symbols(repoId(), query.value)).items
    else if (searchMode.value === 'text') textHits.value = (await api.searchText(repoId(), query.value)).matches
    else relevantHits.value = (await api.relevantFiles(repoId(), query.value)).items
    searched.value = true
  } catch (cause) {
    searchError.value = errorMessage(cause)
  } finally {
    searching.value = false
  }
}

function loadTab(): void {
  if (!workspace.repositoryId) return
  if (tab.value === 'overview' && !context.data.value) void context.load()
  if (tab.value === 'files' && !tree.data.value) void tree.load()
  if (tab.value === 'routes' && !routes.data.value) void routes.load()
  if (tab.value === 'unused' && !unused.data.value) void unused.load()
}

function reset(): void {
  context.data.value = null
  tree.data.value = null
  routes.data.value = null
  unused.data.value = null
  file.value = null
  treePath.value = '.'
  searched.value = false
  loadTab()
}

onMounted(loadTab)
watch(tab, loadTab)
watch(() => workspace.repositoryId, reset)
</script>

<template>
  <PageHeader
    title="Code analysis"
    :subtitle="workspace.repository ? `What the index knows about ${workspace.repository.name}. This is the same information the agent starts from.` : undefined"
  >
    <StatusBadge v-if="workspace.repository" :status="workspace.repository.status" />
  </PageHeader>

  <AsyncBlock
    :loading="false"
    :error="null"
    :empty="!workspace.repository"
    empty-title="No repository selected"
    empty-text="Choose a repository in the top bar, or register one under Projects & repositories."
  >
    <NoticeBox v-if="workspace.repository?.status === 'indexing'" tone="info" class="mb-3">Indexing is still running; results may be incomplete.</NoticeBox>
    <div class="tabs" role="tablist">
      <button
        v-for="option in TABS"
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

    <section v-if="tab === 'overview'" class="mt-4" role="tabpanel">
      <AsyncBlock :loading="context.loading.value" :error="context.error.value" @retry="context.load">
        <div v-if="context.data.value" class="grid gap-4 lg:grid-cols-3">
          <div class="card p-4 lg:col-span-1">
            <h2>Detected stack</h2>
            <dl class="mt-3 space-y-3">
              <div>
                <dt class="label">Kind</dt>
                <dd>{{ context.data.value.profile.kind ?? 'unknown' }}</dd>
              </div>
              <div>
                <dt class="label">Languages (files)</dt>
                <dd class="flex flex-wrap gap-1.5">
                  <span v-for="(count, language) in context.data.value.profile.languages ?? {}" :key="language" class="rounded bg-raised px-1.5 py-0.5 text-xs">
                    {{ language }} {{ count }}
                  </span>
                </dd>
              </div>
              <div>
                <dt class="label">Frameworks and tools</dt>
                <dd>{{ (context.data.value.profile.frameworks ?? []).join(', ') || 'None detected' }}</dd>
              </div>
              <div>
                <dt class="label">Test frameworks</dt>
                <dd>
                  <span v-if="!(context.data.value.profile.test_frameworks ?? []).length">None detected</span>
                  <span v-for="t in context.data.value.profile.test_frameworks ?? []" :key="t.framework + t.directory" class="block">
                    {{ t.framework }} <span class="text-muted">in {{ t.directory }}</span>
                  </span>
                </dd>
              </div>
              <div>
                <dt class="label">Entry points</dt>
                <dd class="font-mono text-xs">
                  <button v-for="entry in context.data.value.profile.entry_points ?? []" :key="entry" type="button" class="link block text-left" @click="openFile(entry)">
                    {{ entry }}
                  </button>
                  <span v-if="!(context.data.value.profile.entry_points ?? []).length" class="font-sans text-sm">None detected</span>
                </dd>
              </div>
            </dl>
            <NoticeBox v-if="context.data.value.profile.truncated" tone="warn" class="mt-3">
              The repository has more files than the indexing limit; only part of it is indexed.
            </NoticeBox>
          </div>
          <div class="card p-4 lg:col-span-2">
            <h2>Repository context given to the agent</h2>
            <pre class="mt-3 overflow-x-auto whitespace-pre-wrap">{{ context.data.value.overview }}</pre>
          </div>
        </div>
      </AsyncBlock>
    </section>

    <section v-if="tab === 'files'" class="mt-4 grid gap-4 lg:grid-cols-3" role="tabpanel">
      <div class="card lg:col-span-1">
        <div class="flex items-center justify-between gap-2 border-b border-line bg-raised px-4 py-2.5">
          <code class="truncate">{{ treePath === '.' ? '/' : treePath }}</code>
          <button v-if="treePath !== '.'" type="button" class="btn btn-sm" @click="openDirectory(parentOf(treePath))">Up</button>
        </div>
        <AsyncBlock :loading="tree.loading.value" :error="tree.error.value" :empty="!tree.data.value?.entries.length" empty-title="Empty directory" @retry="tree.load">
          <ul class="max-h-[32rem] overflow-y-auto py-1 font-mono text-xs">
            <li v-for="entry in tree.data.value?.entries ?? []" :key="entry.path">
              <button
                type="button"
                class="flex w-full items-center gap-2 px-3 py-1 text-left hover:bg-raised"
                :class="{ 'bg-accent-soft': file?.path === entry.path }"
                @click="entry.type === 'dir' ? openDirectory(entry.path) : openFile(entry.path)"
              >
                <span class="w-3 text-muted" aria-hidden="true">{{ entry.type === 'dir' ? '▸' : '' }}</span>
                <span class="truncate" :class="{ 'font-semibold': entry.type === 'dir' }">{{ entry.name }}{{ entry.type === 'dir' ? '/' : '' }}</span>
              </button>
            </li>
          </ul>
          <p v-if="tree.data.value?.hidden_entries" class="border-t border-line px-3 py-2 text-xs text-muted">
            {{ tree.data.value.hidden_entries }} entries hidden (ignored directories and secret files).
          </p>
        </AsyncBlock>
      </div>
      <div class="card min-w-0 lg:col-span-2">
        <AsyncBlock
          :loading="fileLoading"
          :error="fileError"
          :empty="!file"
          empty-title="Select a file"
          empty-text="Secret files such as .env are never shown, and secret-looking values inside files are redacted."
          @retry="file && openFile(file.path)"
        >
          <template v-if="file">
            <div class="flex flex-wrap items-center gap-2 border-b border-line bg-raised px-4 py-2.5">
              <code class="font-medium break-all">{{ file.path }}</code>
              <span class="text-xs text-muted">lines {{ file.start_line }}–{{ file.end_line }} of {{ file.total_lines }}</span>
              <span v-if="file.redacted" class="rounded bg-warn-soft px-1.5 text-xs text-warn">secrets redacted</span>
              <span class="ml-auto flex gap-1">
                <button v-if="file.start_line > 1" type="button" class="btn btn-sm" @click="openFile(file.path, Math.max(1, file.start_line - 780))">Earlier</button>
                <button v-if="file.truncated" type="button" class="btn btn-sm" @click="openFile(file.path, file.end_line + 21)">Later</button>
              </span>
            </div>
            <div class="max-h-[36rem] overflow-auto">
              <table class="w-full border-collapse font-mono text-[0.8125rem] leading-5">
                <tbody>
                  <tr v-for="(line, index) in file.content.split('\n')" :key="index">
                    <td class="w-12 border-r border-line px-2 text-right text-muted select-none">{{ file.start_line + index }}</td>
                    <td class="px-3 whitespace-pre">{{ line }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </template>
        </AsyncBlock>
      </div>
    </section>

    <section v-if="tab === 'search'" class="mt-4" role="tabpanel">
      <form class="card grid gap-2 p-4 sm:grid-cols-[12rem_1fr_auto] sm:items-end" @submit.prevent="search">
        <div>
          <label class="label" for="search-mode">Search in</label>
          <select id="search-mode" v-model="searchMode" class="input" @change="searched = false">
            <option value="symbols">Functions, classes, components</option>
            <option value="text">File contents</option>
            <option value="relevant">Relevant files for a request</option>
          </select>
        </div>
        <div>
          <label class="label" for="search-query">{{ searchMode === 'relevant' ? 'Describe what you are looking for' : 'Search term' }}</label>
          <input id="search-query" v-model="query" class="input" maxlength="300" required />
        </div>
        <button class="btn btn-primary" type="submit" :disabled="searching">{{ searching ? 'Searching…' : 'Search' }}</button>
      </form>
      <div class="card mt-4">
        <AsyncBlock
          :loading="searching"
          :error="searchError"
          :empty="!searched || (searchMode === 'symbols' ? !symbolHits.length : searchMode === 'text' ? !textHits.length : !relevantHits.length)"
          :empty-title="searched ? 'No matches' : 'Run a search'"
          :empty-text="searched ? 'Try a different term, or search file contents instead of symbols.' : 'Symbols come from the index; content search covers every tracked text file.'"
          @retry="search"
        >
          <div class="overflow-x-auto">
            <table v-if="searchMode === 'symbols'" class="table">
              <thead><tr><th>Symbol</th><th>Kind</th><th>Location</th></tr></thead>
              <tbody>
                <tr v-for="hit in pagedSymbols.rows" :key="hit.path + hit.start_line + hit.name">
                  <td>
                    <span class="font-medium">{{ hit.name }}</span>
                    <code class="block max-w-xl truncate text-xs text-muted">{{ hit.signature }}</code>
                  </td>
                  <td>{{ hit.kind }}</td>
                  <td><button type="button" class="link font-mono text-xs" @click="openFile(hit.path, hit.start_line)">{{ hit.path }}:{{ hit.start_line }}</button></td>
                </tr>
              </tbody>
            </table>
            <table v-else-if="searchMode === 'text'" class="table">
              <thead><tr><th>Location</th><th>Line</th></tr></thead>
              <tbody>
                <tr v-for="hit in pagedText.rows" :key="hit.path + hit.line">
                  <td class="whitespace-nowrap"><button type="button" class="link font-mono text-xs" @click="openFile(hit.path, hit.line)">{{ hit.path }}:{{ hit.line }}</button></td>
                  <td><code class="break-all">{{ hit.text }}</code></td>
                </tr>
              </tbody>
            </table>
            <table v-else class="table">
              <thead><tr><th>File</th><th>Score</th><th>Why</th></tr></thead>
              <tbody>
                <tr v-for="hit in pagedRelevant.rows" :key="hit.path">
                  <td><button type="button" class="link font-mono text-xs" @click="openFile(hit.path)">{{ hit.path }}</button></td>
                  <td>{{ hit.score }}</td>
                  <td class="text-xs text-muted">{{ hit.reasons.join('; ') }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <TablePager v-if="searchMode === 'symbols'" v-model="pagedSymbols.page" :total="pagedSymbols.total" />
          <TablePager v-else-if="searchMode === 'text'" v-model="pagedText.page" :total="pagedText.total" />
          <TablePager v-else v-model="pagedRelevant.page" :total="pagedRelevant.total" />
        </AsyncBlock>
      </div>
    </section>

    <section v-if="tab === 'routes'" class="card mt-4" role="tabpanel">
      <AsyncBlock
        :loading="routes.loading.value"
        :error="routes.error.value"
        :empty="!routes.data.value?.items.length"
        empty-title="No routes found"
        empty-text="Route discovery covers FastAPI/Flask decorators, Express-style routers and Vue Router paths."
        @retry="routes.load"
      >
        <div class="overflow-x-auto">
          <table class="table">
            <thead><tr><th>Method</th><th>Path</th><th>Handler</th><th>Location</th></tr></thead>
            <tbody>
              <tr v-for="route in pagedRoutes.rows" :key="route.file + route.line + route.method">
                <td><span class="rounded bg-raised px-1.5 py-0.5 font-mono text-xs">{{ route.method }}</span></td>
                <td><code>{{ route.path }}</code></td>
                <td>{{ route.handler }}</td>
                <td><button type="button" class="link font-mono text-xs" @click="openFile(route.file, route.line)">{{ route.file }}:{{ route.line }}</button></td>
              </tr>
            </tbody>
          </table>
        </div>
        <TablePager v-model="pagedRoutes.page" :total="pagedRoutes.total" />
      </AsyncBlock>
    </section>

    <section v-if="tab === 'unused'" class="mt-4 space-y-4" role="tabpanel">
      <AsyncBlock :loading="unused.loading.value" :error="unused.error.value" @retry="unused.load">
        <template v-if="unused.data.value">
          <NoticeBox tone="neutral">
            Static analysis of {{ unused.data.value.files_analysed }} files. {{ unused.data.value.notes.join(' ') }}
          </NoticeBox>
          <div class="card">
            <h2 class="card-head">Unused imports ({{ unused.data.value.unused_imports.length }})</h2>
            <p v-if="!unused.data.value.unused_imports.length" class="p-4 text-muted">None found.</p>
            <div v-else class="overflow-x-auto">
              <table class="table">
                <thead><tr><th>Name</th><th>Statement</th><th>Location</th><th>Confidence</th></tr></thead>
                <tbody>
                  <tr v-for="item in pagedImports.rows" :key="item.path + item.line + item.name">
                    <td class="font-medium">{{ item.name }}</td>
                    <td><code class="text-xs">{{ item.statement }}</code></td>
                    <td><button type="button" class="link font-mono text-xs" @click="openFile(item.path, item.line)">{{ item.path }}:{{ item.line }}</button></td>
                    <td>{{ item.confidence }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <TablePager v-model="pagedImports.page" :total="pagedImports.total" />
          </div>
          <div class="card">
            <h2 class="card-head">Unreferenced definitions ({{ unused.data.value.unused_definitions.length }})</h2>
            <p v-if="!unused.data.value.unused_definitions.length" class="p-4 text-muted">None found.</p>
            <div v-else class="overflow-x-auto">
              <table class="table">
                <thead><tr><th>Name</th><th>Kind</th><th>Location</th><th>Confidence</th></tr></thead>
                <tbody>
                  <tr v-for="item in pagedDefinitions.rows" :key="item.path + item.line + item.name">
                    <td class="font-medium">{{ item.name }}</td>
                    <td>{{ item.kind }}</td>
                    <td><button type="button" class="link font-mono text-xs" @click="openFile(item.path, item.line)">{{ item.path }}:{{ item.line }}</button></td>
                    <td>{{ item.confidence }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <TablePager v-model="pagedDefinitions.page" :total="pagedDefinitions.total" />
          </div>
        </template>
      </AsyncBlock>
    </section>
  </AsyncBlock>
</template>
