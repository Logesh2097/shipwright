<script setup lang="ts">
import { computed } from 'vue'

import { parseUnifiedDiff } from '@/lib/diff'

const props = defineProps<{ diff: string; path: string; changeType?: string }>()

const parsed = computed(() => parseUnifiedDiff(props.diff))

const LINE_CLASS = { add: 'bg-diff-add', del: 'bg-diff-del', context: '', meta: 'text-muted italic' } as const
const MARKER = { add: '+', del: '-', context: ' ', meta: '\\' } as const
</script>

<template>
  <section class="card overflow-hidden" :aria-label="`Changes to ${path}`">
    <header class="flex flex-wrap items-center gap-2 border-b border-line bg-raised px-4 py-2.5">
      <code class="font-medium break-all">{{ path }}</code>
      <span v-if="changeType === 'create'" class="chip bg-ok-soft text-ok">new file</span>
      <span v-else-if="changeType === 'delete'" class="chip bg-bad-soft text-bad">deleted</span>
      <span class="ml-auto font-mono text-xs">
        <span class="text-ok">+{{ parsed.additions }}</span>
        <span class="ml-2 text-bad">−{{ parsed.deletions }}</span>
      </span>
    </header>
    <p v-if="!parsed.hunks.length" class="p-3 text-muted">No textual changes to show.</p>
    <div v-else class="overflow-x-auto">
      <table class="w-full border-collapse font-mono text-[0.8125rem] leading-5">
        <tbody v-for="(hunk, hunkIndex) in parsed.hunks" :key="hunkIndex">
          <tr class="bg-accent-soft text-accent">
            <td colspan="4" class="px-3 py-0.5 select-none">{{ hunk.header }}</td>
          </tr>
          <tr v-for="(line, lineIndex) in hunk.lines" :key="lineIndex" :class="LINE_CLASS[line.kind]">
            <td class="w-12 border-r border-line px-2 text-right text-muted select-none">{{ line.oldNumber ?? '' }}</td>
            <td class="w-12 border-r border-line px-2 text-right text-muted select-none">{{ line.newNumber ?? '' }}</td>
            <td class="w-5 px-1 text-center select-none" :class="{ 'text-ok': line.kind === 'add', 'text-bad': line.kind === 'del' }">
              {{ MARKER[line.kind] }}
            </td>
            <td class="pr-3 whitespace-pre">{{ line.text }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
