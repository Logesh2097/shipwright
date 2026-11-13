import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api'
import { tokenStore } from '@/api/client'
import type { User } from '@/api/types'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)
  const ready = ref(false)
  const isAuthenticated = computed(() => user.value !== null)
  const isAdmin = computed(() => user.value?.role === 'admin')
  const isViewer = computed(() => user.value?.role === 'viewer')

  // If a refresh fails elsewhere the token store is cleared; mirror that here.
  tokenStore.subscribe((tokens) => {
    if (!tokens) user.value = null
  })

  async function restore(): Promise<void> {
    if (tokenStore.get()) {
      try {
        user.value = await api.me()
      } catch {
        tokenStore.set(null)
        user.value = null
      }
    }
    ready.value = true
  }

  async function login(email: string, password: string): Promise<void> {
    tokenStore.set(await api.login(email, password))
    user.value = await api.me()
  }

  async function register(email: string, fullName: string, password: string): Promise<void> {
    await api.register(email, fullName, password)
    await login(email, password)
  }

  function logout(): void {
    tokenStore.set(null)
    user.value = null
  }

  return { user, ready, isAuthenticated, isAdmin, isViewer, restore, login, register, logout }
})
