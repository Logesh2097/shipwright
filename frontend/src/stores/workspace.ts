import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api'
import { errorMessage } from '@/api/client'
import type { Project, Repository } from '@/api/types'

const PROJECT_KEY = 'asea.project'
const REPOSITORY_KEY = 'asea.repository'

function remember(key: string, value: string | null): void {
  try {
    if (value) localStorage.setItem(key, value)
    else localStorage.removeItem(key)
  } catch {
    // A remembered selection is a convenience only.
  }
}

function recall(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

/** Projects, repositories and the current selection shared by all screens. */
export const useWorkspaceStore = defineStore('workspace', () => {
  const projects = ref<Project[]>([])
  const repositories = ref<Repository[]>([])
  const projectId = ref<string | null>(recall(PROJECT_KEY))
  const repositoryId = ref<string | null>(recall(REPOSITORY_KEY))
  const loading = ref(false)
  const loaded = ref(false)
  const error = ref<string | null>(null)

  const project = computed(() => projects.value.find((p) => p.id === projectId.value) ?? null)
  const repository = computed(() => repositories.value.find((r) => r.id === repositoryId.value) ?? null)
  const role = computed(() => project.value?.my_role ?? null)
  const canWrite = computed(() => role.value === 'owner' || role.value === 'developer')
  const canManage = computed(() => role.value === 'owner')

  async function loadRepositories(): Promise<void> {
    if (!projectId.value) {
      repositories.value = []
      repositoryId.value = null
      return
    }
    repositories.value = await api.repositories(projectId.value)
    if (!repositories.value.some((r) => r.id === repositoryId.value)) {
      repositoryId.value = repositories.value[0]?.id ?? null
      remember(REPOSITORY_KEY, repositoryId.value)
    }
  }

  async function load(): Promise<void> {
    loading.value = true
    try {
      projects.value = (await api.projects()).items
      if (!projects.value.some((p) => p.id === projectId.value)) {
        projectId.value = projects.value[0]?.id ?? null
        remember(PROJECT_KEY, projectId.value)
      }
      await loadRepositories()
      error.value = null
    } catch (cause) {
      error.value = errorMessage(cause)
    } finally {
      loading.value = false
      loaded.value = true
    }
  }

  async function selectProject(id: string | null): Promise<void> {
    projectId.value = id
    remember(PROJECT_KEY, id)
    try {
      await loadRepositories()
    } catch (cause) {
      error.value = errorMessage(cause)
    }
  }

  function selectRepository(id: string | null): void {
    repositoryId.value = id
    remember(REPOSITORY_KEY, id)
  }

  function reset(): void {
    projects.value = []
    repositories.value = []
    loaded.value = false
  }

  function repositoryName(id: string | null | undefined): string {
    return repositories.value.find((r) => r.id === id)?.name ?? '—'
  }

  return {
    projects, repositories, projectId, repositoryId, project, repository, role, canWrite, canManage,
    loading, loaded, error, load, loadRepositories, selectProject, selectRepository, reset, repositoryName,
  }
})
