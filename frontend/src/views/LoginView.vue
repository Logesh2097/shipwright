<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { errorMessage } from '@/api/client'
import NoticeBox from '@/components/NoticeBox.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const mode = ref<'login' | 'register'>('login')
const email = ref('')
const fullName = ref('')
const password = ref('')
const busy = ref(false)
const error = ref<string | null>(null)

async function submit(): Promise<void> {
  busy.value = true
  error.value = null
  try {
    if (mode.value === 'login') await auth.login(email.value, password.value)
    else await auth.register(email.value, fullName.value, password.value)
    const next = typeof route.query.next === 'string' && route.query.next.startsWith('/') ? route.query.next : '/'
    await router.push(next)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="screen-backdrop flex min-h-screen items-center justify-center p-4">
    <div class="card w-full max-w-sm p-6">
      <div class="flex items-center gap-2">
        <span class="mdi mdi-sail-boat text-3xl leading-none text-brand" aria-hidden="true"></span>
        <h1>Shipwright</h1>
      </div>
      <p class="mt-1 text-muted">AI coding agent that works under your approval. Sign in to continue.</p>

      <div class="seg w-full mt-5" role="tablist">
        <button
          v-for="option in (['login', 'register'] as const)"
          :key="option"
          type="button"
          role="tab"
          class="seg__btn"
          :aria-selected="mode === option"
          @click="mode = option; error = null"
        >
          {{ option === 'login' ? 'Sign in' : 'Create account' }}
        </button>
      </div>

      <form class="mt-4 space-y-3" @submit.prevent="submit">
        <div>
          <label class="label" for="email">Email</label>
          <input id="email" v-model.trim="email" class="input" type="email" autocomplete="username" required />
        </div>
        <div v-if="mode === 'register'">
          <label class="label" for="name">Name</label>
          <input id="name" v-model.trim="fullName" class="input" type="text" autocomplete="name" maxlength="200" />
        </div>
        <div>
          <label class="label" for="password">Password</label>
          <input
            id="password"
            v-model="password"
            class="input"
            type="password"
            :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
            :minlength="mode === 'register' ? 10 : undefined"
            required
          />
          <p v-if="mode === 'register'" class="mt-1 text-xs text-muted">At least 10 characters.</p>
        </div>
        <NoticeBox v-if="error" tone="bad">{{ error }}</NoticeBox>
        <button class="btn btn-primary w-full" type="submit" :disabled="busy">
          {{ busy ? 'Please wait…' : mode === 'login' ? 'Sign in' : 'Create account' }}
        </button>
      </form>
      <p v-if="mode === 'register'" class="mt-3 text-xs text-muted">
        The first account created on a new installation becomes the administrator. Later accounts start as developers.
      </p>
    </div>
  </main>
</template>
