<script setup lang="ts">
// The three states every data area has besides its content: loading, failed, empty.
// Loading is a skeleton on the neutral fill; failed and empty are one quiet figure with a
// sentence, and a retry where there is something to retry.
defineProps<{
  loading: boolean
  error: string | null
  empty?: boolean
  emptyTitle?: string
  emptyText?: string
}>()
defineEmits<{ retry: [] }>()
</script>

<template>
  <div v-if="loading" class="skeleton" role="status" aria-live="polite" aria-busy="true">
    <span class="skeleton__line" aria-hidden="true"></span>
    <span class="skeleton__line" aria-hidden="true"></span>
    <span class="skeleton__line" aria-hidden="true"></span>
    <span class="sr-only">Loading…</span>
  </div>
  <div v-else-if="error" class="state-block" role="alert">
    <span class="state-block__icon mdi mdi-connection" aria-hidden="true"></span>
    <p class="font-semibold">Could not load this data</p>
    <p class="state-block__text">{{ error }}</p>
    <button class="btn btn-sm" type="button" @click="$emit('retry')">
      <span class="mdi mdi-refresh text-base" aria-hidden="true"></span>
      Try again
    </button>
  </div>
  <div v-else-if="empty" class="state-block">
    <span class="state-block__icon mdi mdi-tray" aria-hidden="true"></span>
    <p class="font-semibold">{{ emptyTitle ?? 'Nothing here yet' }}</p>
    <p v-if="emptyText" class="state-block__text">{{ emptyText }}</p>
    <div v-if="$slots.emptyAction" class="mt-2"><slot name="emptyAction" /></div>
  </div>
  <slot v-else />
</template>
