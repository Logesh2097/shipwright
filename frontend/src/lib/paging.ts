import { computed, reactive, ref, watch } from 'vue'

// Tables show five rows at a time. Pages are numbered from 1.
export const PAGE_SIZE = 5

export function pageCount(total: number, size = PAGE_SIZE): number {
  return Math.max(1, Math.ceil(total / size))
}

/** `page` kept inside 1..pageCount. */
export function clampPage(page: number, total: number, size = PAGE_SIZE): number {
  if (!Number.isFinite(page)) return 1
  return Math.min(pageCount(total, size), Math.max(1, Math.trunc(page)))
}

/** The rows of one page. */
export function pageOf<T>(rows: readonly T[], page: number, size = PAGE_SIZE): T[] {
  const start = (clampPage(page, rows.length, size) - 1) * size
  return rows.slice(start, start + size)
}

/** "6–10 of 23": which rows are on screen. */
export function rangeText(total: number, page: number, size = PAGE_SIZE): string {
  if (total <= 0) return '0 of 0'
  const current = clampPage(page, total, size)
  const first = (current - 1) * size + 1
  return `${first}–${Math.min(total, current * size)} of ${total}`
}

/**
 * A list shown one page at a time, for lists that are already in the browser.
 * When the list shrinks (a filter, a refresh) the page is pulled back into range;
 * it is not reset, so a background refresh does not move the reader.
 */
export function usePaged<T>(source: () => readonly T[] | null | undefined, size = PAGE_SIZE) {
  const page = ref(1)
  const all = computed(() => source() ?? [])
  const total = computed(() => all.value.length)
  const rows = computed(() => pageOf(all.value, page.value, size))
  watch(total, (count) => {
    page.value = clampPage(page.value, count, size)
  })
  return reactive({ page, rows, total })
}
