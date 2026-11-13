<script setup lang="ts">
import type { Tone } from '@/lib/format'

// A notice: a tonal wash with ink words. Its colour is only in the bar at the start and the icon.
withDefaults(defineProps<{ tone?: Tone; title?: string }>(), { tone: 'info', title: undefined })

const LOOK: Record<Tone, { hue: string; words: string; icon: string }> = {
  neutral: { hue: 'var(--color-fill-ink)', words: 'var(--color-muted)', icon: 'mdi-information-outline' },
  ok: { hue: 'var(--color-ok-hue)', words: 'var(--color-ok)', icon: 'mdi-check-circle-outline' },
  warn: { hue: 'var(--color-warn-hue)', words: 'var(--color-warn)', icon: 'mdi-alert-outline' },
  bad: { hue: 'var(--color-bad-hue)', words: 'var(--color-bad)', icon: 'mdi-alert-circle-outline' },
  info: { hue: 'var(--color-brand)', words: 'var(--color-accent)', icon: 'mdi-information-outline' },
  accent: { hue: 'var(--color-brand)', words: 'var(--color-accent)', icon: 'mdi-information-outline' },
}
</script>

<template>
  <div
    class="notice"
    :style="{ '--notice-hue': LOOK[tone].hue, '--notice-words': LOOK[tone].words }"
    :role="tone === 'bad' ? 'alert' : 'status'"
  >
    <span class="notice__icon mdi" :class="LOOK[tone].icon" aria-hidden="true"></span>
    <div class="notice__body">
      <p v-if="title" class="font-semibold">{{ title }}</p>
      <div :class="{ 'mt-0.5': title }"><slot /></div>
    </div>
  </div>
</template>
