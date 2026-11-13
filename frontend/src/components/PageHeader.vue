<script setup lang="ts">
// The top of every page: the title on the page ground, an optional second line, and the
// page's controls on the right (the one primary action lives here, not in the bar).
defineProps<{ title: string; subtitle?: string }>()
</script>

<template>
  <header class="page-header">
    <div class="min-w-0">
      <h1 class="page-header__title">{{ title }}</h1>
      <p v-if="subtitle" class="page-header__sub">{{ subtitle }}</p>
    </div>
    <div v-if="$slots.default" class="page-header__actions"><slot /></div>
  </header>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 16px;
}
.page-header__title {
  overflow-wrap: anywhere;
}
.page-header__sub {
  margin-top: 2px;
  max-width: 78ch;
  font-size: var(--text-xs);
  line-height: 1.45;
  color: var(--color-muted);
}
.page-header__actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
@media (max-width: 600px) {
  .page-header {
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
    margin-bottom: 12px;
  }
  .page-header__title {
    font-size: var(--text-lg);
    line-height: 1.35;
  }
  .page-header__actions {
    width: 100%;
  }
  .page-header__actions :deep(> .btn) {
    flex: 1 1 132px;
    min-width: 0;
  }
}
</style>
