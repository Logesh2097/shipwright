<script setup lang="ts">
import { computed } from 'vue'

import { renderMarkdown } from '@/lib/markdown'

const props = defineProps<{ source: string }>()
// renderMarkdown escapes all input and emits a fixed set of tags; see lib/markdown.ts and its tests.
const html = computed(() => renderMarkdown(props.source))
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html -->
  <div class="prose-report break-words" v-html="html"></div>
</template>
