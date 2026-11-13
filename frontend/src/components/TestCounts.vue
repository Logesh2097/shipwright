<script setup lang="ts">
// The five outcomes are always shown separately: "not executed" must never read as "passed".
defineProps<{ passed: number; failed: number; skipped: number; errors: number; notExecuted: number }>()
</script>

<template>
  <span class="inline-flex flex-wrap items-center gap-x-3 gap-y-0.5 font-mono text-xs">
    <span class="text-ok" title="Passed">{{ passed }} passed</span>
    <span :class="failed ? 'text-bad font-semibold' : 'text-muted'" title="Failed">{{ failed }} failed</span>
    <span class="text-muted" title="Skipped by the test suite">{{ skipped }} skipped</span>
    <span :class="errors ? 'text-bad font-semibold' : 'text-muted'" title="Errors">{{ errors }} errors</span>
    <span :class="notExecuted ? 'text-warn font-semibold' : 'text-muted'" title="Selected but not run">{{ notExecuted }} not executed</span>
  </span>
</template>
