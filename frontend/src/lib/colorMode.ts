import { ref } from 'vue'

// Light or dark. The remembered choice wins; without one, what the device prefers.
export type ColorMode = 'light' | 'dark'

const KEY = 'asea.theme'

function preferred(): ColorMode {
  try {
    const saved = localStorage.getItem(KEY)
    if (saved === 'light' || saved === 'dark') return saved
  } catch {
    // private browsing: fall through to the device preference
  }
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export const colorMode = ref<ColorMode>('light')

function apply(mode: ColorMode): void {
  colorMode.value = mode
  document.documentElement.dataset.theme = mode
}

/** Called once before the app is mounted, so the first paint is already in the right mode. */
export function initColorMode(): void {
  apply(preferred())
}

export function setColorMode(mode: ColorMode): void {
  apply(mode)
  try {
    localStorage.setItem(KEY, mode)
  } catch {
    // private browsing: the choice lasts for this visit only
  }
}
