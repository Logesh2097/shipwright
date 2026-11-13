<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api } from '@/api'
import { errorMessage } from '@/api/client'
import type { GitHubIssue, GitHubPull, GitHubRepo } from '@/api/types'
import AsyncBlock from '@/components/AsyncBlock.vue'
import NoticeBox from '@/components/NoticeBox.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { formatRelative } from '@/lib/format'
import { useAsync } from '@/lib/useAsync'
import { useWorkspaceStore } from '@/stores/workspace'

const workspace = useWorkspaceStore()
const router = useRouter()
const repoId = () => workspace.repositoryId ?? ''

// --- local Git state of the selected repository
const status = useAsync(() => api.gitStatus(repoId()))
const branches = useAsync(() => api.gitBranches(repoId()))
const log = useAsync(() => api.gitLog(repoId()))

// --- requests for write operations (each becomes a pending approval)
const message = ref<{ tone: 'ok' | 'bad'; text: string } | null>(null)
const busy = ref(false)
const branchName = ref('')
const commitMessage = ref('')
const commitPaths = ref<string[]>([])
const pushBranch = ref('')
const pr = ref({ head: '', base: '', title: '', body: '', draft: false })

async function requestOperation(action: () => Promise<unknown>, label: string): Promise<void> {
  busy.value = true
  message.value = null
  try {
    await action()
    message.value = { tone: 'ok', text: `${label} requested. It will run only after it is approved on the Approvals screen.` }
  } catch (cause) {
    message.value = { tone: 'bad', text: errorMessage(cause) }
  } finally {
    busy.value = false
  }
}

const changedPaths = computed(() => (status.data.value?.entries ?? []).filter((e) => e.state !== 'conflicted').map((e) => e.path))
const localBranches = computed(() => (branches.data.value?.branches ?? []).filter((b) => !b.remote))
const pushable = computed(() => localBranches.value.filter((b) => b.name !== workspace.repository?.default_branch))

// --- GitHub
const github = useAsync(() => api.githubStatus())
const remoteRepos = useAsync<{ repositories: GitHubRepo[] }>(() => api.githubRepos())
const selectedRemote = ref<string | null>(null)
const issues = useAsync<{ issues: GitHubIssue[] }>(() => api.githubIssues(selectedRemote.value ?? ''))
const pulls = useAsync<{ pull_requests: GitHubPull[] }>(() => api.githubPulls(selectedRemote.value ?? ''))

function selectRemote(fullName: string): void {
  selectedRemote.value = fullName
  void issues.load()
  void pulls.load()
}

function loadLocal(): void {
  message.value = null
  commitPaths.value = []
  if (!workspace.repositoryId) return
  void status.load()
  void branches.load().then(() => {
    const current = branches.data.value?.current ?? ''
    pushBranch.value = pushable.value.some((b) => b.name === current) ? current : (pushable.value[0]?.name ?? '')
    pr.value.head = pushBranch.value
    pr.value.base = workspace.repository?.default_branch ?? ''
  })
  void log.load()
}

onMounted(async () => {
  loadLocal()
  await github.load()
  if (github.data.value?.configured) {
    await remoteRepos.load()
    const linked = workspace.repository?.github_full_name
    if (linked) selectRemote(linked)
  }
})
watch(() => workspace.repositoryId, loadLocal)
</script>

<template>
  <PageHeader
    title="Git & GitHub"
    subtitle="Reading is immediate. Creating branches, committing, pushing and opening pull requests each create a request that must be approved before anything happens. Nothing is ever merged, force-pushed, or pushed to the default branch."
  />
  <NoticeBox v-if="message" :tone="message.tone" class="mb-4">
    {{ message.text }}
    <button v-if="message.tone === 'ok'" type="button" class="link ml-1" @click="router.push('/approvals')">Go to approvals</button>
  </NoticeBox>

  <AsyncBlock :loading="false" :error="null" :empty="!workspace.repository" empty-title="No repository selected" empty-text="Choose a repository in the top bar to see its Git state.">
    <div class="grid gap-4 lg:grid-cols-2">
      <section class="card">
        <h2 class="card-head">Working tree of {{ workspace.repository?.name }}</h2>
        <AsyncBlock :loading="status.loading.value" :error="status.error.value" @retry="status.load">
          <div v-if="status.data.value" class="p-4">
            <p>
              On branch <code class="font-medium">{{ status.data.value.branch ?? '(detached)' }}</code>
              <span v-if="status.data.value.tracking" class="text-muted"> tracking {{ status.data.value.tracking }}</span>
            </p>
            <p v-if="status.data.value.clean" class="mt-2 text-muted">Clean: no uncommitted changes.</p>
            <ul v-else class="mt-2 max-h-56 space-y-1 overflow-y-auto font-mono text-xs">
              <li v-for="entry in status.data.value.entries" :key="entry.path" class="flex items-center gap-2">
                <label class="flex min-w-0 flex-1 items-center gap-2">
                  <input v-if="workspace.canWrite && entry.state !== 'conflicted'" v-model="commitPaths" type="checkbox" :value="entry.path" />
                  <span class="truncate">{{ entry.path }}</span>
                </label>
                <span class="text-muted">{{ entry.state }}</span>
              </li>
            </ul>
            <form
              v-if="workspace.canWrite && changedPaths.length"
              class="mt-3 space-y-2 border-t border-line pt-3"
              @submit.prevent="requestOperation(() => api.requestCommit(repoId(), commitPaths, commitMessage), 'Commit')"
            >
              <h3>Request a commit</h3>
              <p class="text-xs text-muted">Tick the files above. Only those paths are staged and committed; repository hooks are not run.</p>
              <label class="label" for="commit-message">Commit message</label>
              <textarea id="commit-message" v-model="commitMessage" class="input" rows="2" minlength="3" maxlength="5000" required></textarea>
              <button class="btn" type="submit" :disabled="busy || !commitPaths.length || commitMessage.trim().length < 3">
                Request commit of {{ commitPaths.length }} file(s)
              </button>
            </form>
          </div>
        </AsyncBlock>
      </section>

      <section class="card">
        <h2 class="card-head">Branches and history</h2>
        <AsyncBlock :loading="branches.loading.value" :error="branches.error.value" @retry="branches.load">
          <div class="p-4">
            <ul class="flex flex-wrap gap-1.5">
              <li v-for="branch in branches.data.value?.branches ?? []" :key="branch.name">
                <span class="rounded border border-line px-1.5 py-0.5 font-mono text-xs" :class="{ 'border-accent bg-accent-soft text-accent': branch.current }">
                  {{ branch.name }}
                </span>
              </li>
            </ul>
            <form
              v-if="workspace.canWrite"
              class="mt-3 flex flex-wrap items-end gap-2"
              @submit.prevent="requestOperation(() => api.requestBranch(repoId(), branchName), 'Branch creation')"
            >
              <div class="min-w-48 flex-1">
                <label class="label" for="branch-name">New branch from the current HEAD (uncommitted work is carried over)</label>
                <input id="branch-name" v-model.trim="branchName" class="input font-mono" placeholder="fix/discount-rounding" pattern="[A-Za-z0-9][A-Za-z0-9._\/\-]*" required />
              </div>
              <button class="btn" type="submit" :disabled="busy || !branchName">Request branch</button>
            </form>
            <h3 class="mt-4">Recent commits</h3>
            <p v-if="!log.data.value?.commits.length" class="mt-1 text-muted">No commits yet.</p>
            <ul v-else class="mt-1 max-h-48 space-y-1 overflow-y-auto text-xs">
              <li v-for="commit in log.data.value?.commits ?? []" :key="commit.sha" class="flex gap-2">
                <code class="text-muted">{{ commit.sha.slice(0, 8) }}</code>
                <span class="min-w-0 flex-1 truncate">{{ commit.subject }}</span>
                <span class="shrink-0 text-muted">{{ commit.author }} · {{ formatRelative(commit.date) }}</span>
              </li>
            </ul>
          </div>
        </AsyncBlock>
      </section>
    </div>

    <section v-if="workspace.canWrite" class="card mt-4">
      <h2 class="card-head">Push and pull request</h2>
      <div class="p-4">
        <NoticeBox v-if="!workspace.repository?.github_full_name" tone="neutral">
          This repository has no GitHub remote named <code>origin</code>, so pushing and pull requests are not available for it.
        </NoticeBox>
        <NoticeBox v-else-if="github.data.value && !github.data.value.configured" tone="warn">
          Add a GitHub token under Settings to push or open pull requests for <code>{{ workspace.repository.github_full_name }}</code>.
        </NoticeBox>
        <div v-else class="grid gap-6 lg:grid-cols-2">
          <form class="space-y-2" @submit.prevent="requestOperation(() => api.requestPush(repoId(), pushBranch), 'Push')">
            <h3>Request a push to {{ workspace.repository.github_full_name }}</h3>
            <label class="label" for="push-branch">Branch (the default branch “{{ workspace.repository.default_branch }}” cannot be pushed)</label>
            <select id="push-branch" v-model="pushBranch" class="input font-mono" required>
              <option v-if="!pushable.length" value="">No other local branch</option>
              <option v-for="branch in pushable" :key="branch.name" :value="branch.name">{{ branch.name }}</option>
            </select>
            <button class="btn" type="submit" :disabled="busy || !pushBranch">Request push</button>
          </form>
          <form class="space-y-2" @submit.prevent="requestOperation(() => api.requestPullRequest(repoId(), { ...pr, base: pr.base || undefined }), 'Pull request')">
            <h3>Request a pull request</h3>
            <div class="grid grid-cols-2 gap-2">
              <div>
                <label class="label" for="pr-head">From branch (must already be pushed)</label>
                <input id="pr-head" v-model.trim="pr.head" class="input font-mono" required />
              </div>
              <div>
                <label class="label" for="pr-base">Into</label>
                <input id="pr-base" v-model.trim="pr.base" class="input font-mono" :placeholder="workspace.repository.default_branch ?? ''" />
              </div>
            </div>
            <label class="label" for="pr-title">Title</label>
            <input id="pr-title" v-model.trim="pr.title" class="input" maxlength="300" required />
            <label class="label" for="pr-body">Description</label>
            <textarea id="pr-body" v-model="pr.body" class="input" rows="3" maxlength="60000"></textarea>
            <label class="flex items-center gap-2"><input v-model="pr.draft" type="checkbox" /> Open as draft</label>
            <button class="btn" type="submit" :disabled="busy || !pr.head || !pr.title">Request pull request</button>
          </form>
        </div>
      </div>
    </section>
  </AsyncBlock>

  <section class="card mt-4">
    <h2 class="card-head">GitHub</h2>
    <AsyncBlock :loading="github.loading.value" :error="github.error.value" @retry="github.load">
      <AsyncBlock
        :loading="false"
        :error="null"
        :empty="!github.data.value?.configured"
        empty-title="GitHub is not connected"
        empty-text="Store a fine-grained personal access token under Settings to browse repositories, issues and pull requests."
      >
        <template #emptyAction><RouterLink class="btn" to="/settings">Open settings</RouterLink></template>
        <div class="grid gap-0 lg:grid-cols-3">
          <div class="border-b border-line lg:border-r lg:border-b-0">
            <p class="px-4 py-2 text-xs text-muted">Connected as {{ github.data.value?.login ?? 'unknown' }} (token {{ github.data.value?.hint }})</p>
            <AsyncBlock
              :loading="remoteRepos.loading.value"
              :error="remoteRepos.error.value"
              :empty="!remoteRepos.data.value?.repositories.length"
              empty-title="No repositories visible to this token"
              @retry="remoteRepos.load"
            >
              <ul class="max-h-96 overflow-y-auto">
                <li v-for="repo in remoteRepos.data.value?.repositories ?? []" :key="repo.full_name">
                  <button
                    type="button"
                    class="block w-full px-4 py-2 text-left hover:bg-raised"
                    :class="{ 'bg-accent-soft': selectedRemote === repo.full_name }"
                    @click="selectRemote(repo.full_name)"
                  >
                    <span class="block truncate font-medium">{{ repo.full_name }}</span>
                    <span class="block truncate text-xs text-muted">
                      {{ repo.private ? 'private' : 'public' }} · {{ repo.default_branch }} · {{ repo.language ?? 'n/a' }} · pushed {{ formatRelative(repo.pushed_at) }}
                    </span>
                  </button>
                </li>
              </ul>
            </AsyncBlock>
          </div>
          <div class="min-w-0 lg:col-span-2">
            <AsyncBlock :loading="false" :error="null" :empty="!selectedRemote" empty-title="Select a repository" empty-text="Its open issues and pull requests are shown here.">
              <div class="grid gap-0 md:grid-cols-2">
                <div class="border-b border-line md:border-r md:border-b-0">
                  <h3 class="px-4 py-2">Open issues</h3>
                  <AsyncBlock :loading="issues.loading.value" :error="issues.error.value" :empty="!issues.data.value?.issues.length" empty-title="No open issues" @retry="issues.load">
                    <ul class="max-h-80 overflow-y-auto">
                      <li v-for="issue in issues.data.value?.issues ?? []" :key="issue.number" class="border-t border-line px-4 py-2">
                        <a :href="issue.html_url" target="_blank" rel="noopener noreferrer" class="link">#{{ issue.number }} {{ issue.title }}</a>
                        <p class="text-xs text-muted">{{ issue.user }} · {{ formatRelative(issue.updated_at) }} <span v-if="issue.labels.length">· {{ issue.labels.join(', ') }}</span></p>
                      </li>
                    </ul>
                  </AsyncBlock>
                </div>
                <div>
                  <h3 class="px-4 py-2">Open pull requests</h3>
                  <AsyncBlock :loading="pulls.loading.value" :error="pulls.error.value" :empty="!pulls.data.value?.pull_requests.length" empty-title="No open pull requests" @retry="pulls.load">
                    <ul class="max-h-80 overflow-y-auto">
                      <li v-for="pull in pulls.data.value?.pull_requests ?? []" :key="pull.number" class="border-t border-line px-4 py-2">
                        <a :href="pull.html_url" target="_blank" rel="noopener noreferrer" class="link">#{{ pull.number }} {{ pull.title }}</a>
                        <p class="text-xs text-muted">
                          <code>{{ pull.head }}</code> → <code>{{ pull.base }}</code> · {{ pull.user }}
                          <StatusBadge v-if="pull.draft" status="draft" class="ml-1" />
                        </p>
                      </li>
                    </ul>
                  </AsyncBlock>
                </div>
              </div>
            </AsyncBlock>
          </div>
        </div>
      </AsyncBlock>
    </AsyncBlock>
  </section>
</template>
