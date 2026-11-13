<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { api } from '@/api'
import { errorMessage } from '@/api/client'
import type { User } from '@/api/types'
import AsyncBlock from '@/components/AsyncBlock.vue'
import NoticeBox from '@/components/NoticeBox.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import TablePager from '@/components/TablePager.vue'
import { formatDateTime, titleCase } from '@/lib/format'
import { usePaged } from '@/lib/paging'
import { useAsync } from '@/lib/useAsync'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const system = useAsync(() => api.systemSettings())
const github = useAsync(() => api.githubStatus())
const users = useAsync(() => api.users())
const pagedExamples = usePaged(() => system.data.value?.command_policy_examples)
const pagedUsers = usePaged(() => users.data.value?.items)

const token = ref('')
const busy = ref(false)
const message = ref<{ tone: 'ok' | 'bad'; text: string } | null>(null)

onMounted(() => {
  void system.load()
  void github.load()
  if (auth.isAdmin) void users.load()
})

async function act(action: () => Promise<unknown>, success: string): Promise<void> {
  busy.value = true
  message.value = null
  try {
    await action()
    message.value = { tone: 'ok', text: success }
  } catch (cause) {
    message.value = { tone: 'bad', text: errorMessage(cause) }
  } finally {
    busy.value = false
  }
}

async function saveToken(): Promise<void> {
  await act(async () => {
    await api.setGithubToken(token.value)
    token.value = ''
    await github.refresh()
  }, 'GitHub token validated and stored (encrypted). It is never shown again.')
}

async function removeToken(): Promise<void> {
  if (!window.confirm('Remove the stored GitHub token?')) return
  await act(async () => {
    await api.deleteGithubToken()
    await github.refresh()
  }, 'GitHub token removed')
}

async function updateUser(user: User, change: Partial<Pick<User, 'role' | 'is_active'>>): Promise<void> {
  await act(async () => {
    await api.updateUser(user.id, change)
    await users.refresh()
  }, `Updated ${user.email}`)
}
</script>

<template>
  <PageHeader title="Settings & permissions" subtitle="Your account, GitHub access, and the rules this installation enforces." />
  <NoticeBox v-if="message" :tone="message.tone" class="mb-4">{{ message.text }}</NoticeBox>

  <div class="grid gap-4 lg:grid-cols-2">
    <section class="card p-4">
      <h2>Your account</h2>
      <dl class="mt-3 space-y-2">
        <div class="flex gap-3"><dt class="w-24 text-muted">Name</dt><dd>{{ auth.user?.full_name || '—' }}</dd></div>
        <div class="flex gap-3"><dt class="w-24 text-muted">Email</dt><dd>{{ auth.user?.email }}</dd></div>
        <div class="flex gap-3"><dt class="w-24 text-muted">Global role</dt><dd>{{ auth.user?.role }}</dd></div>
      </dl>
      <p class="mt-3 text-xs text-muted">Your role inside each project is shown under Projects & repositories. A global viewer is read-only everywhere.</p>
    </section>

    <section class="card p-4">
      <h2>GitHub access</h2>
      <AsyncBlock :loading="github.loading.value" :error="github.error.value" @retry="github.load">
        <template v-if="github.data.value">
          <p v-if="github.data.value.configured" class="mt-2">
            Connected as <strong>{{ github.data.value.login ?? 'unknown' }}</strong> · token {{ github.data.value.hint }} · saved
            {{ formatDateTime(github.data.value.updated_at) }}
          </p>
          <p v-else class="mt-2 text-muted">No token stored for your account.</p>
          <form v-if="!auth.isViewer" class="mt-3 space-y-2" @submit.prevent="saveToken">
            <label class="label" for="github-token">{{ github.data.value.configured ? 'Replace token' : 'Fine-grained personal access token' }}</label>
            <input id="github-token" v-model.trim="token" class="input font-mono" type="password" autocomplete="off" minlength="20" placeholder="github_pat_…" required />
            <div class="flex flex-wrap gap-2">
              <button class="btn btn-primary" type="submit" :disabled="busy || token.length < 20">Validate and save</button>
              <button v-if="github.data.value.configured" class="btn btn-danger" type="button" :disabled="busy" @click="removeToken">Remove token</button>
            </div>
          </form>
          <div class="mt-3 text-xs text-muted">
            <p>Create a fine-grained token limited to the repositories you need, with these repository permissions:</p>
            <ul class="mt-1 list-disc pl-5">
              <li>Metadata: read · Contents: read and write (write is only needed for pushing branches)</li>
              <li>Issues: read · Pull requests: read and write (write is only needed for opening pull requests)</li>
            </ul>
            <p class="mt-1">The token is checked against GitHub, encrypted at rest, used only by your own requests, and never sent to the language model.</p>
          </div>
        </template>
      </AsyncBlock>
    </section>
  </div>

  <AsyncBlock :loading="system.loading.value" :error="system.error.value" class="mt-4" @retry="system.load">
    <template v-if="system.data.value">
      <div class="mt-4 grid gap-4 lg:grid-cols-2">
        <section class="card p-4">
          <h2>System</h2>
          <dl class="mt-3 space-y-2">
            <div class="flex gap-3">
              <dt class="w-40 shrink-0 text-muted">Language model</dt>
              <dd>
                <StatusBadge :status="system.data.value.llm.configured ? 'success' : 'not_executed'" :label="system.data.value.llm.configured ? 'Configured' : 'Not configured'" />
                <span v-if="system.data.value.llm.model" class="ml-2 font-mono text-xs">{{ system.data.value.llm.provider }} / {{ system.data.value.llm.model }}</span>
              </dd>
            </div>
            <div class="flex gap-3">
              <dt class="w-40 shrink-0 text-muted">Sandbox</dt>
              <dd>
                <StatusBadge :status="system.data.value.sandbox.mode === 'docker' ? 'success' : system.data.value.sandbox.mode === 'local' ? 'not_executed' : 'failed'" :label="system.data.value.sandbox.mode" />
                <span class="ml-2 text-xs text-muted">{{ system.data.value.sandbox.detail }}</span>
                <p class="mt-1 text-xs text-muted">
                  Limits per command: {{ system.data.value.sandbox.memory_mb }} MB memory, {{ system.data.value.sandbox.cpus }} CPU,
                  {{ system.data.value.sandbox.pids_limit }} processes, no network.
                </p>
              </dd>
            </div>
            <div class="flex gap-3">
              <dt class="w-40 shrink-0 text-muted">Repository roots</dt>
              <dd class="font-mono text-xs">
                <span v-if="!system.data.value.repositories.allowed_roots.length" class="font-sans text-sm text-warn">None configured: repositories cannot be registered.</span>
                <span v-for="root in system.data.value.repositories.allowed_roots" :key="root" class="block">{{ root }}</span>
              </dd>
            </div>
            <div class="flex gap-3">
              <dt class="w-40 shrink-0 text-muted">Agent limits</dt>
              <dd>{{ system.data.value.agent.max_tool_steps }} tool steps per stage · {{ system.data.value.agent.max_repair_attempts }} repair attempts</dd>
            </div>
            <div class="flex gap-3">
              <dt class="w-40 shrink-0 text-muted">Approvals</dt>
              <dd>Requests expire after {{ system.data.value.approvals.expiry_hours }} h · write authorisations are valid for {{ system.data.value.approvals.token_ttl_seconds }} s</dd>
            </div>
            <div class="flex gap-3">
              <dt class="w-40 shrink-0 text-muted">Self-registration</dt>
              <dd>{{ system.data.value.registration_open ? 'Open' : 'Closed' }} · environment: {{ system.data.value.environment }}</dd>
            </div>
          </dl>
          <p class="mt-3 text-xs text-muted">These values come from the server's environment and are changed there, not in the browser.</p>
        </section>

        <section class="card p-4">
          <h2>Roles</h2>
          <dl class="mt-3 space-y-2">
            <div v-for="(description, role) in system.data.value.roles" :key="role" class="flex gap-3">
              <dt class="w-24 shrink-0 font-medium">{{ role }}</dt>
              <dd class="text-muted">{{ description }}</dd>
            </div>
          </dl>
          <h2 class="mt-5">What always needs approval</h2>
          <ul class="mt-2 list-disc space-y-0.5 pl-5">
            <li v-for="(label, key) in system.data.value.approvals.actions" :key="key">{{ label }}</li>
          </ul>
          <p class="mt-2 text-xs text-muted">There is no capability to merge pull requests, force-push, push to the default branch, or deploy.</p>
        </section>

        <section class="card p-4 lg:col-span-2">
          <h2>Tool risk levels and command policy</h2>
          <dl class="mt-3 grid gap-2 sm:grid-cols-2">
            <div v-for="(description, level) in system.data.value.risk_levels" :key="level" class="flex gap-2">
              <dt class="w-20 shrink-0"><StatusBadge :status="String(level)" /></dt>
              <dd class="text-muted">{{ description }}</dd>
            </div>
          </dl>
          <h3 class="mt-4">How example commands are classified</h3>
          <div class="mt-1 overflow-x-auto">
            <table class="table">
              <thead><tr><th>Command</th><th>Decision</th><th>Reason</th></tr></thead>
              <tbody>
                <tr v-for="example in pagedExamples.rows" :key="example.command">
                  <td><code>{{ example.command }}</code></td>
                  <td><StatusBadge :status="example.level" :label="example.level === 'safe' ? 'Runs in sandbox' : example.level === 'sensitive' ? 'Needs approval' : 'Denied'" /></td>
                  <td class="text-muted">{{ example.reason }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <TablePager v-model="pagedExamples.page" :total="pagedExamples.total" />
          <p class="mt-2 text-xs text-muted">Commands are argument lists, never shell strings. Anything not on the allowlist is denied.</p>
        </section>
      </div>
    </template>
  </AsyncBlock>

  <section v-if="auth.isAdmin" class="card mt-4">
    <h2 class="card-head">Users</h2>
    <AsyncBlock :loading="users.loading.value" :error="users.error.value" @retry="users.load">
      <div class="overflow-x-auto">
        <table class="table">
          <thead><tr><th>User</th><th>Global role</th><th>Status</th><th>Created</th></tr></thead>
          <tbody>
            <tr v-for="user in pagedUsers.rows" :key="user.id">
              <td>
                <p>{{ user.full_name || user.email }}</p>
                <p class="text-xs text-muted">{{ user.email }}</p>
              </td>
              <td>
                <select
                  class="input input-sm w-32"
                  :value="user.role"
                  :aria-label="`Role of ${user.email}`"
                  :disabled="busy"
                  @change="updateUser(user, { role: ($event.target as HTMLSelectElement).value as User['role'] })"
                >
                  <option v-for="role in ['admin', 'developer', 'viewer']" :key="role" :value="role">{{ titleCase(role) }}</option>
                </select>
              </td>
              <td>
                <label class="flex items-center gap-2">
                  <input
                    type="checkbox"
                    :checked="user.is_active"
                    :disabled="busy || user.id === auth.user?.id"
                    @change="updateUser(user, { is_active: ($event.target as HTMLInputElement).checked })"
                  />
                  {{ user.is_active ? 'Active' : 'Disabled' }}
                </label>
              </td>
              <td class="text-xs text-muted">{{ formatDateTime(user.created_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <TablePager v-model="pagedUsers.page" :total="pagedUsers.total" />
    </AsyncBlock>
  </section>
</template>
