import { onBeforeUnmount, ref, type Ref } from 'vue'

import { errorMessage } from '@/api/client'

export interface AsyncState<T> {
  data: Ref<T | null>
  loading: Ref<boolean>
  error: Ref<string | null>
  load: () => Promise<void>
  /** Reload without flipping back to the loading state (for polling). */
  refresh: () => Promise<void>
}

/** Loading / error / data state for one request, with stale-response protection. */
export function useAsync<T>(fetcher: () => Promise<T>): AsyncState<T> {
  const data = ref<T | null>(null) as Ref<T | null>
  const loading = ref(false)
  const error = ref<string | null>(null)
  let sequence = 0

  async function run(showLoading: boolean): Promise<void> {
    const current = ++sequence
    if (showLoading) loading.value = true
    try {
      const result = await fetcher()
      if (current !== sequence) return
      data.value = result
      error.value = null
    } catch (cause) {
      if (current !== sequence) return
      // During background refreshes keep showing the last good data; surface the error only if there is none.
      if (showLoading || data.value === null) error.value = errorMessage(cause)
    } finally {
      if (current === sequence) loading.value = false
    }
  }

  return { data, loading, error, load: () => run(true), refresh: () => run(false) }
}

/** Call ``fn`` every ``ms`` while the component is mounted and the tab is visible. */
export function usePolling(fn: () => void | Promise<void>, ms: number): void {
  const timer = window.setInterval(() => {
    if (document.visibilityState === 'visible') void fn()
  }, ms)
  onBeforeUnmount(() => window.clearInterval(timer))
}
