<script setup>
import { computed, ref } from 'vue'
import { RefreshCw } from '@lucide/vue'
import { api } from '../api'
import { dateTime, number } from '../lib/format'
import { useAutoRefresh } from '../lib/autoRefresh'

const data = ref(null)
const loading = ref(false)

const tone = (ok, warn = false) => (ok ? (warn ? 'warn' : 'ok') : 'error')

const items = computed(() => {
  const pulse = data.value
  if (!pulse) return [
    { label: 'CPA', value: '读取中', tone: 'warn' },
    { label: '上游账号', value: '读取中', tone: 'warn' },
    { label: '同步 Worker', value: '读取中', tone: 'warn' },
    { label: '上游额度', value: '读取中', tone: 'warn' },
  ]
  const quotaTotal = pulse.accounts?.total
  const quotaNormal = pulse.accounts?.normal
  const quotaWarn = Number(pulse.accounts?.limit_reached || 0) > 0 || Number(pulse.accounts?.failed || 0) > 0
  return [
    {
      label: 'CPA',
      value: pulse.cpa?.reachable ? `${number(pulse.cpa.api_key_count)} Keys · ${pulse.cpa.latency_ms} ms` : '管理接口不可用',
      tone: tone(Boolean(pulse.cpa?.reachable)),
    },
    {
      label: '上游账号',
      value: pulse.accounts?.available ? `${number(quotaTotal)} 个已读取` : '不可用',
      tone: tone(Boolean(pulse.accounts?.available), quotaWarn),
    },
    {
      label: '同步 Worker',
      value: pulse.worker?.healthy ? `Backlog ${number(pulse.worker.backlog)}` : '同步异常',
      tone: tone(Boolean(pulse.worker?.healthy), Number(pulse.worker?.backlog || 0) > 0),
    },
    {
      label: '上游额度',
      value: quotaTotal === undefined || quotaTotal === null ? '暂无缓存' : `${quotaNormal}/${quotaTotal} 正常`,
      tone: tone(Boolean(pulse.accounts?.available), quotaWarn),
    },
  ]
})

async function load() {
  if (loading.value) return
  loading.value = true
  try {
    data.value = await api('/api/site/pulse')
  } catch {
    data.value = {
      cpa: { reachable: false },
      accounts: { available: false },
      worker: { healthy: false },
    }
  } finally {
    loading.value = false
  }
}

const autoRefresh = useAutoRefresh(() => load(), { interval: 60_000 })
</script>

<template>
  <div class="pulse-track">
    <div class="pulse-track__inner">
      <div v-for="item in items" :key="item.label" class="pulse-track__item">
        <span class="status-dot" :class="`status-dot--${item.tone}`" />
        <div class="min-w-0">
          <div class="pulse-track__label">{{ item.label }}</div>
          <div class="pulse-track__value">{{ item.value }}</div>
        </div>
      </div>
    </div>
    <v-tooltip text="刷新系统状态" location="bottom">
      <template #activator="{ props }">
        <v-btn v-bind="props" class="pulse-refresh" icon size="x-small" variant="text" :loading="loading" @click="autoRefresh.refresh()">
          <RefreshCw :size="15" />
        </v-btn>
      </template>
    </v-tooltip>
  </div>
</template>

<style scoped>
.pulse-track { position: relative; }
.pulse-refresh { position: absolute; right: 7px; top: 6px; background: rgba(238, 243, 241, 0.92); }
.min-w-0 { min-width: 0; }
</style>
