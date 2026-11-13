<script setup lang="ts">
import { computed, ref } from 'vue'

import { api } from '@/api'
import { errorMessage } from '@/api/client'
import type { Approval } from '@/api/types'
import { formatDateTime } from '@/lib/format'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'

import NoticeBox from './NoticeBox.vue'
import StatusBadge from './StatusBadge.vue'

const props = defineProps<{ approval: Approval; staleFiles?: { path: string; reason: string }[] }>()
const emit = defineEmits<{ decided: [approval: Approval] }>()

const workspace = useWorkspaceStore()
const auth = useAuthStore()
const comment = ref('')
const confirmDeletions = ref(false)
const busy = ref<'approve' | 'reject' | null>(null)
const error = ref<string | null>(null)

const deletes = computed(() => (Array.isArray(props.approval.payload.deletes) ? (props.approval.payload.deletes as string[]) : []))
const isPending = computed(() => props.approval.status === 'pending')
const expired = computed(() => !!props.approval.expires_at && new Date(props.approval.expires_at) < new Date())
// The role is known for the selected project; for requests from other projects the server decides.
const mayDecide = computed(() =>
  workspace.repositories.some((r) => r.id === props.approval.repository_id) ? workspace.canWrite : auth.user?.role !== 'viewer',
)
const canDecide = computed(() => mayDecide.value && isPending.value && !expired.value)
const approveBlocked = computed(() => deletes.value.length > 0 && !confirmDeletions.value)

async function decide(kind: 'approve' | 'reject'): Promise<void> {
  busy.value = kind
  error.value = null
  try {
    const result =
      kind === 'approve' ? await api.approve(props.approval.id, comment.value, confirmDeletions.value) : await api.reject(props.approval.id, comment.value)
    emit('decided', result)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <section class="card p-4" aria-label="Approval decision">
    <div class="flex flex-wrap items-center gap-2">
      <h2>{{ approval.title }}</h2>
      <StatusBadge :status="approval.status" />
    </div>
    <p class="mt-1 text-muted">
      Requested {{ formatDateTime(approval.created_at) }} by {{ approval.requested_by_user_id ? 'a developer' : 'the agent' }}
      <template v-if="isPending && approval.expires_at"> · expires {{ formatDateTime(approval.expires_at) }}</template>
      <template v-if="approval.decided_at"> · decided {{ formatDateTime(approval.decided_at) }}</template>
    </p>
    <p v-if="approval.comment" class="mt-2">Comment: “{{ approval.comment }}”</p>

    <template v-if="isPending">
      <NoticeBox v-if="staleFiles?.length" tone="bad" title="These files changed after the proposal was made" class="mt-3">
        <ul class="list-disc pl-5">
          <li v-for="file in staleFiles" :key="file.path">
            <code>{{ file.path }}</code> — {{ file.reason }}
          </li>
        </ul>
        <p class="mt-1">If you approve, nothing will be written and the change set will be marked stale. Your edits are never overwritten.</p>
      </NoticeBox>

      <NoticeBox v-if="deletes.length" tone="warn" title="This change deletes files" class="mt-3">
        <ul class="list-disc pl-5">
          <li v-for="path in deletes" :key="path">
            <code>{{ path }}</code>
          </li>
        </ul>
        <label class="mt-2 flex items-center gap-2 text-fg">
          <input v-model="confirmDeletions" type="checkbox" :disabled="!canDecide" />
          I confirm these files should be deleted
        </label>
      </NoticeBox>

      <NoticeBox v-if="expired" tone="warn" class="mt-3">This request has expired and can no longer be decided.</NoticeBox>
      <NoticeBox v-else-if="!mayDecide" tone="neutral" class="mt-3">
        You have read-only access to this project. A developer or owner has to decide this request.
      </NoticeBox>

      <div v-if="canDecide" class="mt-3">
        <label class="label" :for="`comment-${approval.id}`">Comment (optional)</label>
        <textarea :id="`comment-${approval.id}`" v-model="comment" class="input" rows="2" maxlength="2000"></textarea>
        <NoticeBox v-if="error" tone="bad" class="mt-2">{{ error }}</NoticeBox>
        <div class="mt-3 flex flex-wrap gap-2">
          <button class="btn btn-primary" type="button" :disabled="busy !== null || approveBlocked" @click="decide('approve')">
            {{ busy === 'approve' ? 'Approving…' : 'Approve' }}
          </button>
          <button class="btn btn-danger" type="button" :disabled="busy !== null" @click="decide('reject')">
            {{ busy === 'reject' ? 'Rejecting…' : 'Reject' }}
          </button>
          <span v-if="approveBlocked" class="self-center text-xs text-muted">Confirm the deletions to enable approval.</span>
        </div>
      </div>
    </template>
  </section>
</template>
