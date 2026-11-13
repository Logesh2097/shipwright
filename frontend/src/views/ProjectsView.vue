<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { api } from '@/api'
import { errorMessage } from '@/api/client'
import type { Member, ProjectRole } from '@/api/types'
import AsyncBlock from '@/components/AsyncBlock.vue'
import NoticeBox from '@/components/NoticeBox.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import TablePager from '@/components/TablePager.vue'
import { formatRelative } from '@/lib/format'
import { usePaged } from '@/lib/paging'
import { useAsync, usePolling } from '@/lib/useAsync'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'

const workspace = useWorkspaceStore()
const auth = useAuthStore()

const message = ref<{ tone: 'ok' | 'bad'; text: string } | null>(null)
const busy = ref(false)

async function act(action: () => Promise<unknown>, success: string): Promise<boolean> {
  busy.value = true
  message.value = null
  try {
    await action()
    message.value = { tone: 'ok', text: success }
    return true
  } catch (cause) {
    message.value = { tone: 'bad', text: errorMessage(cause) }
    return false
  } finally {
    busy.value = false
  }
}

// --- projects
const newProject = ref({ name: '', description: '' })
async function createProject(): Promise<void> {
  const name = newProject.value.name
  if (
    await act(async () => {
      const project = await api.createProject(name, newProject.value.description)
      await workspace.load()
      await workspace.selectProject(project.id)
    }, `Project “${name}” created`)
  )
    newProject.value = { name: '', description: '' }
}

async function deleteProject(): Promise<void> {
  const project = workspace.project
  if (!project) return
  if (!window.confirm(`Delete project “${project.name}” and all of its records? Repository files on disk are not touched.`)) return
  await act(async () => {
    await api.deleteProject(project.id)
    await workspace.load()
  }, 'Project deleted')
}

async function setAutoRun(value: boolean): Promise<void> {
  const project = workspace.project
  if (!project) return
  await act(async () => {
    await api.updateProject(project.id, { settings: { auto_run_tests: value } })
    await workspace.load()
  }, value ? 'The agent will run tests automatically after applying approved changes' : 'Automatic test runs disabled for this project')
}

// --- repositories
const newRepo = ref({ path: '', name: '' })
async function registerRepository(): Promise<void> {
  const projectId = workspace.projectId
  if (!projectId) return
  if (
    await act(async () => {
      const repository = await api.registerRepository(projectId, newRepo.value.path, newRepo.value.name)
      await workspace.loadRepositories()
      workspace.selectRepository(repository.id)
    }, 'Repository registered; indexing has started')
  )
    newRepo.value = { path: '', name: '' }
}

async function reindex(id: string): Promise<void> {
  await act(async () => {
    await api.reindex(id)
    await workspace.loadRepositories()
  }, 'Re-indexing started')
}

async function unregister(id: string, name: string): Promise<void> {
  if (!window.confirm(`Unregister “${name}”? Its tasks and history are removed. Files on disk are not touched.`)) return
  await act(async () => {
    await api.unregisterRepository(id)
    await workspace.loadRepositories()
  }, 'Repository unregistered')
}

const indexing = computed(() => workspace.repositories.some((r) => r.status === 'indexing'))
usePolling(() => {
  if (indexing.value) void workspace.loadRepositories().catch(() => undefined)
}, 2000)

// --- members
const members = useAsync<Member[]>(() => (workspace.projectId ? api.members(workspace.projectId) : Promise.resolve([])))
const pagedMembers = usePaged(() => members.data.value)
const newMember = ref<{ email: string; role: ProjectRole }>({ email: '', role: 'developer' })
watch(() => workspace.projectId, () => void members.load(), { immediate: false })
onMounted(async () => {
  if (!workspace.loaded) await workspace.load()
  await members.load()
})

async function addMember(): Promise<void> {
  const projectId = workspace.projectId
  if (!projectId) return
  if (await act(() => api.upsertMember(projectId, newMember.value.email, newMember.value.role), 'Member saved')) {
    newMember.value = { email: '', role: 'developer' }
    await members.refresh()
  }
}

async function changeRole(member: Member, role: string): Promise<void> {
  const projectId = workspace.projectId
  if (!projectId) return
  await act(() => api.upsertMember(projectId, member.email, role as ProjectRole), 'Role updated')
  await members.refresh()
}

async function removeMember(member: Member): Promise<void> {
  const projectId = workspace.projectId
  if (!projectId || !window.confirm(`Remove ${member.email} from this project?`)) return
  await act(() => api.removeMember(projectId, member.user_id), 'Member removed')
  await members.refresh()
}
</script>

<template>
  <PageHeader title="Projects & repositories" subtitle="A project groups repositories and the people who may work on them." />
  <NoticeBox v-if="message" :tone="message.tone" class="mb-4">{{ message.text }}</NoticeBox>

  <div class="grid gap-4 lg:grid-cols-3">
    <section class="card lg:col-span-1">
      <h2 class="card-head">Projects</h2>
      <AsyncBlock
        :loading="workspace.loading && !workspace.loaded"
        :error="workspace.error"
        :empty="!workspace.projects.length"
        empty-title="No projects yet"
        :empty-text="auth.isViewer ? 'Ask a project owner to add you to a project.' : 'Create your first project below.'"
        @retry="workspace.load"
      >
        <ul>
          <li v-for="project in workspace.projects" :key="project.id" class="border-b border-line last:border-b-0">
            <button
              type="button"
              class="flex w-full items-center justify-between gap-2 px-4 py-2.5 text-left hover:bg-raised"
              :class="{ 'bg-accent-soft': project.id === workspace.projectId }"
              :aria-pressed="project.id === workspace.projectId"
              @click="workspace.selectProject(project.id)"
            >
              <span class="min-w-0">
                <span class="block truncate font-medium">{{ project.name }}</span>
                <span class="block truncate text-xs text-muted">{{ project.repository_count }} repositories · you are {{ project.my_role }}</span>
              </span>
            </button>
          </li>
        </ul>
      </AsyncBlock>
      <form v-if="!auth.isViewer" class="space-y-2 border-t border-line p-4" @submit.prevent="createProject">
        <h3>New project</h3>
        <div>
          <label class="label" for="project-name">Name</label>
          <input id="project-name" v-model.trim="newProject.name" class="input" required maxlength="200" />
        </div>
        <div>
          <label class="label" for="project-description">Description (optional)</label>
          <input id="project-description" v-model.trim="newProject.description" class="input" maxlength="5000" />
        </div>
        <button class="btn btn-primary" type="submit" :disabled="busy || !newProject.name">Create project</button>
      </form>
    </section>

    <div class="space-y-4 lg:col-span-2">
      <AsyncBlock
        :loading="false"
        :error="null"
        :empty="!workspace.project"
        empty-title="Select or create a project"
        empty-text="Repositories and members are managed per project."
      >
        <section class="card">
          <div class="card-head flex flex-wrap items-center justify-between gap-2">
            <h2>Repositories in {{ workspace.project?.name }}</h2>
            <span v-if="indexing" class="text-xs text-muted">Indexing in progress…</span>
          </div>
          <AsyncBlock
            :loading="false"
            :error="null"
            :empty="!workspace.repositories.length"
            empty-title="No repositories registered"
            empty-text="Register a local Git repository to let the agent explore it. Nothing is copied or modified by registering."
          >
            <ul class="grid gap-3 p-4 xl:grid-cols-2">
              <li v-for="repository in workspace.repositories" :key="repository.id" class="repo-card">
                <div class="flex items-start gap-3">
                  <span class="repo-card__icon mdi mdi-source-repository" aria-hidden="true"></span>
                  <div class="min-w-0 flex-1">
                    <p class="truncate font-semibold">{{ repository.name }}</p>
                    <code class="block truncate text-xs text-muted" :title="repository.local_path">{{ repository.local_path }}</code>
                  </div>
                  <StatusBadge :status="repository.status" />
                </div>

                <p class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
                  <span class="inline-flex items-center gap-1">
                    <span class="mdi mdi-source-branch text-base leading-none" aria-hidden="true"></span>branch {{ repository.default_branch ?? 'unknown' }}
                  </span>
                  <span v-if="repository.github_full_name" class="inline-flex items-center gap-1">
                    <span class="mdi mdi-github text-base leading-none" aria-hidden="true"></span>GitHub {{ repository.github_full_name }}
                  </span>
                  <span class="inline-flex items-center gap-1">
                    <span class="mdi mdi-file-multiple-outline text-base leading-none" aria-hidden="true"></span>{{ repository.profile.kind ?? '—' }} · {{ repository.profile.file_count ?? 0 }} files
                  </span>
                  <span class="inline-flex items-center gap-1">
                    <span class="mdi mdi-update text-base leading-none" aria-hidden="true"></span>indexed {{ formatRelative(repository.last_indexed_at) }}
                  </span>
                </p>

                <div class="flex flex-wrap gap-1.5">
                  <span v-for="framework in (repository.profile.frameworks ?? []).slice(0, 6)" :key="framework" class="chip bg-raised text-muted">{{ framework }}</span>
                  <span v-if="!(repository.profile.frameworks ?? []).length" class="text-xs text-muted">No frameworks detected</span>
                </div>

                <p v-if="repository.index_error" class="text-xs text-bad">{{ repository.index_error }}</p>

                <div v-if="workspace.canWrite || workspace.canManage" class="repo-card__actions">
                  <button v-if="workspace.canWrite" class="btn btn-sm" type="button" :disabled="busy || repository.status === 'indexing'" @click="reindex(repository.id)">
                    <span class="mdi mdi-refresh text-base leading-none" aria-hidden="true"></span>Re-index
                  </button>
                  <button v-if="workspace.canManage" class="btn btn-sm btn-danger" type="button" :disabled="busy" @click="unregister(repository.id, repository.name)">
                    <span class="mdi mdi-link-variant-off text-base leading-none" aria-hidden="true"></span>Unregister
                  </button>
                </div>
              </li>
            </ul>
          </AsyncBlock>
          <form v-if="workspace.canManage" class="grid gap-2 border-t border-line p-4 sm:grid-cols-[1fr_12rem_auto] sm:items-end" @submit.prevent="registerRepository">
            <div>
              <label class="label" for="repo-path">Absolute path of a Git repository on the server</label>
              <input id="repo-path" v-model.trim="newRepo.path" class="input font-mono" placeholder="/repos/my-service" required />
            </div>
            <div>
              <label class="label" for="repo-name">Display name (optional)</label>
              <input id="repo-name" v-model.trim="newRepo.name" class="input" maxlength="200" />
            </div>
            <button class="btn btn-primary" type="submit" :disabled="busy || !newRepo.path">Register</button>
            <p class="text-xs text-muted sm:col-span-3">
              The path must be the top level of a Git repository inside a directory the server allows (see Settings). Repositories whose Git configuration can execute
              programs are refused.
            </p>
          </form>
          <p v-else class="border-t border-line p-4 text-xs text-muted">Only project owners can register or unregister repositories.</p>
        </section>

        <section class="card">
          <h2 class="card-head card-head--plain">Members and project settings</h2>
          <AsyncBlock :loading="members.loading.value" :error="members.error.value" @retry="members.load">
            <div class="overflow-x-auto">
              <table class="table">
                <thead><tr><th>Member</th><th>Role in project</th><th><span class="sr-only">Actions</span></th></tr></thead>
                <tbody>
                  <tr v-for="member in pagedMembers.rows" :key="member.user_id">
                    <td>
                      <p>{{ member.full_name || member.email }}</p>
                      <p class="text-xs text-muted">{{ member.email }}</p>
                    </td>
                    <td>
                      <select
                        v-if="workspace.canManage"
                        class="input input-sm w-36"
                        :value="member.role"
                        :aria-label="`Role of ${member.email}`"
                        :disabled="busy"
                        @change="changeRole(member, ($event.target as HTMLSelectElement).value)"
                      >
                        <option value="owner">owner</option>
                        <option value="developer">developer</option>
                        <option value="viewer">viewer</option>
                      </select>
                      <span v-else>{{ member.role }}</span>
                    </td>
                    <td class="text-right">
                      <button v-if="workspace.canManage" class="btn btn-sm" type="button" :disabled="busy" @click="removeMember(member)">Remove</button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <TablePager v-model="pagedMembers.page" :total="pagedMembers.total" />
          </AsyncBlock>
          <div v-if="workspace.canManage" class="space-y-4 border-t border-line p-4">
            <form class="grid gap-2 sm:grid-cols-[1fr_9rem_auto] sm:items-end" @submit.prevent="addMember">
              <div>
                <label class="label" for="member-email">Add an existing account by email</label>
                <input id="member-email" v-model.trim="newMember.email" class="input" type="email" required />
              </div>
              <div>
                <label class="label" for="member-role">Role</label>
                <select id="member-role" v-model="newMember.role" class="input">
                  <option value="developer">developer</option>
                  <option value="viewer">viewer</option>
                  <option value="owner">owner</option>
                </select>
              </div>
              <button class="btn" type="submit" :disabled="busy || !newMember.email">Add member</button>
            </form>
            <label class="flex items-start gap-2">
              <input
                type="checkbox"
                class="mt-0.5"
                :checked="workspace.project?.settings.auto_run_tests !== false"
                :disabled="busy"
                @change="setAutoRun(($event.target as HTMLInputElement).checked)"
              />
              <span>
                Run tests automatically after approved changes are applied
                <span class="block text-xs text-muted">Tests run in the sandbox without network access. When off, run checks yourself from the Tests screen.</span>
              </span>
            </label>
            <button class="btn btn-danger" type="button" :disabled="busy" @click="deleteProject">Delete this project</button>
          </div>
        </section>
      </AsyncBlock>
    </div>
  </div>
</template>

<style scoped>
/* One repository: an inset card on the card ground, with its actions on a rule at the foot. */
.repo-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  padding: 16px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--color-surface);
}
.repo-card__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  flex-shrink: 0;
  border-radius: var(--radius-md);
  background: var(--color-accent-soft);
  color: var(--color-accent);
  font-size: 20px;
  line-height: 1;
}
.repo-card__actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
  margin-top: auto;
  padding-top: 12px;
  border-top: 1px solid var(--hairline-soft);
}
</style>
