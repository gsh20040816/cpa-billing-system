<script setup>
import { computed } from 'vue'
import { TIME_RANGE_ITEMS, isValidCustomHours } from '../lib/timeRange'

const props = defineProps({
  modelValue: { type: Object, required: true },
  label: { type: String, default: '时间范围' },
})

const emit = defineEmits(['update:modelValue'])

const range = computed({
  get: () => props.modelValue.range,
  set: (value) => update({ range: value, custom_hours: value === 'custom' ? props.modelValue.custom_hours : '' }),
})
const customHours = computed({
  get: () => props.modelValue.custom_hours,
  set: (value) => update({ custom_hours: value }),
})
const customHoursError = computed(() => (
  range.value === 'custom' && customHours.value !== '' && !isValidCustomHours(customHours.value)
    ? '请输入正整数小时数'
    : ''
))

function update(changes) {
  emit('update:modelValue', { ...props.modelValue, ...changes })
}
</script>

<template>
  <div class="time-range-selector" :aria-label="label">
    <v-btn-toggle v-model="range" mandatory divided color="primary" density="compact">
      <v-btn v-for="item in TIME_RANGE_ITEMS" :key="item.value" :value="item.value" size="small">{{ item.title }}</v-btn>
    </v-btn-toggle>
    <v-text-field
      v-if="range === 'custom'"
      v-model="customHours"
      type="number"
      min="1"
      step="1"
      label="小时数"
      suffix="小时"
      :error-messages="customHoursError ? [customHoursError] : []"
      style="width: 150px"
    />
  </div>
</template>

<style scoped>
.time-range-selector { display: flex; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
.time-range-selector :deep(.v-btn-toggle) { flex-wrap: wrap; }
</style>
