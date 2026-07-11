<script setup>
defineProps({
  items: { type: Array, required: true },
  columns: { type: Number, default: 4 },
})
</script>

<template>
  <div class="metric-rail" :style="{ '--metric-columns': columns }">
    <div v-for="item in items" :key="item.label" class="metric-rail__item">
      <div class="metric-rail__label">{{ item.label }}</div>
      <div class="metric-rail__value" :class="item.mono ? 'mono' : ''">{{ item.value }}</div>
      <div v-if="item.hint" class="metric-rail__hint">{{ item.hint }}</div>
    </div>
  </div>
</template>
