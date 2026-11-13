<script setup lang="ts">
import { computed } from 'vue'

import { titleCase, toneFor, type Tone } from '@/lib/format'

const props = defineProps<{ status: string | null | undefined; tone?: Tone; label?: string }>()

const CLASSES: Record<Tone, string> = {
  neutral: 'bg-raised text-muted',
  ok: 'bg-ok-soft text-ok',
  warn: 'bg-warn-soft text-warn',
  bad: 'bg-bad-soft text-bad',
  info: 'bg-info-soft text-info',
  accent: 'bg-accent-soft text-accent',
}

const tone = computed(() => props.tone ?? toneFor(props.status))
const text = computed(() => props.label ?? titleCase(props.status))
</script>

<template>
  <span class="chip" :class="CLASSES[tone]">
    <span v-if="status === 'running' || status === 'indexing'" class="size-1.5 animate-pulse rounded-full bg-current" aria-hidden="true"></span>
    {{ text }}
  </span>
</template>
