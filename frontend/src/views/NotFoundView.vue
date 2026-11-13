<template>
  <main class="screen-backdrop flex min-h-screen flex-col items-center justify-center gap-3 p-6 text-center">
    <span class="state-block__icon mdi mdi-compass-off-outline" aria-hidden="true"></span>
    <h1>Page not found</h1>
    <p class="text-muted">The address does not match any screen in this application.</p>
    <RouterLink class="btn btn-primary" to="/">Go to the dashboard</RouterLink>
  </main>
</template>
