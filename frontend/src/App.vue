<script setup lang="ts">
import { watch } from 'vue'
import { RouterView, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

// A session that ends while the app is open (expired refresh token) returns to the sign-in page.
watch(
  () => auth.isAuthenticated,
  (authenticated) => {
    if (auth.ready && !authenticated && router.currentRoute.value.name !== 'login') void router.push({ name: 'login' })
  },
)
</script>

<template>
  <RouterView />
</template>
