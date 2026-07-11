<script setup>
import { AlertTriangle, DatabaseZap } from '@lucide/vue'

defineProps({
  loading: Boolean,
  error: { type: String, default: '' },
  empty: Boolean,
  emptyText: { type: String, default: '暂无数据' },
})

defineEmits(['retry'])
</script>

<template>
  <div v-if="loading" class="pa-8 text-center">
    <v-progress-circular indeterminate color="primary" size="28" />
  </div>
  <v-alert v-else-if="error" type="error" variant="tonal" border="start" class="ma-4">
    <template #prepend><AlertTriangle :size="20" /></template>
    <div class="d-flex align-center justify-space-between ga-4 flex-wrap">
      <span>{{ error }}</span>
      <v-btn size="small" variant="outlined" @click="$emit('retry')">重试</v-btn>
    </div>
  </v-alert>
  <div v-else-if="empty" class="pa-8 text-center data-muted">
    <DatabaseZap :size="26" class="mb-2" />
    <div>{{ emptyText }}</div>
  </div>
  <slot v-else />
</template>
