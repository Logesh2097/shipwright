<script setup lang="ts">
import { computed } from 'vue'

import { clampPage, pageCount, PAGE_SIZE, rangeText } from '@/lib/paging'

// The strip under a table: which rows are shown, and the way to the other pages.
// Hidden when everything fits on one page.
const props = withDefaults(defineProps<{ modelValue: number; total: number; size?: number }>(), { size: PAGE_SIZE })
const emit = defineEmits<{ 'update:modelValue': [page: number] }>()

const pages = computed(() => pageCount(props.total, props.size))
const page = computed(() => clampPage(props.modelValue, props.total, props.size))

function go(to: number): void {
  emit('update:modelValue', clampPage(to, props.total, props.size))
}
</script>

<template>
  <nav v-if="pages > 1" class="pager" aria-label="Table pages">
    <span class="pager__range">{{ rangeText(total, page, size) }}</span>
    <button class="pager__btn" type="button" aria-label="First page" :disabled="page === 1" @click="go(1)">
      <span class="mdi mdi-page-first" aria-hidden="true"></span>
    </button>
    <button class="pager__btn" type="button" aria-label="Previous page" :disabled="page === 1" @click="go(page - 1)">
      <span class="mdi mdi-chevron-left" aria-hidden="true"></span>
    </button>
    <span class="pager__page" aria-live="polite">Page {{ page }} of {{ pages }}</span>
    <button class="pager__btn" type="button" aria-label="Next page" :disabled="page === pages" @click="go(page + 1)">
      <span class="mdi mdi-chevron-right" aria-hidden="true"></span>
    </button>
    <button class="pager__btn" type="button" aria-label="Last page" :disabled="page === pages" @click="go(pages)">
      <span class="mdi mdi-page-last" aria-hidden="true"></span>
    </button>
  </nav>
</template>

<style scoped>
.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 2px;
  padding: 6px 8px;
  border-top: 1px solid var(--hairline-soft);
  font-size: var(--text-xs);
  color: var(--color-muted);
  font-variant-numeric: tabular-nums;
}
.pager__range {
  margin-right: auto;
  padding-left: 8px;
}
.pager__page {
  padding: 0 6px;
  white-space: nowrap;
}
.pager__btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: var(--radius-md);
  font-size: 18px;
  line-height: 1;
  color: var(--color-fg);
  cursor: pointer;
  transition: background-color var(--dur-fast) var(--ease-out);
}
.pager__btn:hover:not(:disabled) {
  background: var(--fill-2);
}
.pager__btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
@media (pointer: coarse) {
  .pager__btn {
    width: var(--control-h);
    height: var(--control-h);
  }
}
</style>
