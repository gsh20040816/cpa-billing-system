<script setup>
import { computed, inject, ref, watch } from 'vue'
import { ArrowLeft, KeyRound, RefreshCw } from '@lucide/vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import LoadingState from '../components/LoadingState.vue'
import MetricRail from '../components/MetricRail.vue'
import PageHeader from '../components/PageHeader.vue'
import { useAutoRefresh } from '../lib/autoRefresh'
import { dateTime, money, number } from '../lib/format'
import { toQuery } from '../lib/query'

const route = useRoute()
const router = useRouter()
const session = inject('userSession')
const loading = ref(true)
const error = ref('')
const data = ref(null)
const cycle = ref(String(route.query.cycle || ''))

const displayName = computed(() => data.value?.username ? `@${data.value.username}` : `Telegram ${data.value?.telegram_user_id || route.params.id}`)
const own = computed(() => Number(session?.value?.telegram_user_id) === Number(route.params.id))
const metrics = computed(() => [
  { label: '请求', value: number(data.value?.summary?.requests), mono: true },
  { label: 'Tokens', value: number(data.value?.summary?.tokens), mono: true },
  { label: '请求等效成本', value: money(data.value?.summary?.cost), mono: true },
  { label: '成功率', value: data.value?.summary?.success_rate || '-', mono: true },
  { label: '失败', value: number(data.value?.summary?.failed), mono: true },
  { label: '长上下文', value: number(data.value?.summary?.long_context), mono: true },
])

async function load(silent = false) {
  if (!silent) loading.value = true
  error.value = ''
  try {
    data.value = await api(`/api/users/${route.params.id}/summary${toQuery({ cycle: cycle.value })}`)
    if (!cycle.value && data.value?.cycle?.name) cycle.value = data.value.cycle.name
  } catch (exc) {
    error.value = exc.message
  } finally {
    if (!silent) loading.value = false
  }
}

const autoRefresh = useAutoRefresh((silent) => load(silent), { interval: 30_000 })

watch(cycle, (value, previous) => { if (previous && value !== previous) autoRefresh.refresh() })
</script>

<template>
  <div class="content-shell">
    <PageHeader :title="displayName" :subtitle="data ? `${data.first_name || ''} ${data.last_name || ''}`.trim() : 'Telegram 用户聚合用量'">
      <template #actions>
        <v-btn variant="outlined" @click="router.back()"><ArrowLeft :size="17" class="mr-2" />返回</v-btn>
        <v-btn v-if="own" to="/keys" color="primary"><KeyRound :size="17" class="mr-2" />我的 Key</v-btn>
        <v-select v-if="data?.cycles?.length" v-model="cycle" :items="data.cycles" item-title="name" item-value="name" label="账期" style="width: 210px" />
        <v-tooltip text="刷新用户聚合"><template #activator="{ props }"><v-btn v-bind="props" icon variant="outlined" :loading="loading" @click="autoRefresh.refresh()"><RefreshCw :size="18" /></v-btn></template></v-tooltip>
      </template>
    </PageHeader>

    <LoadingState :loading="loading" :error="error" :empty="!data?.cycle" empty-text="该用户当前没有账期数据" @retry="autoRefresh.refresh()">
      <MetricRail :items="metrics" :columns="6" />
      <v-alert v-if="data?.statement?.live" type="info" variant="tonal" density="compact" class="mb-4">
        当前为开放账期实时估算，更新于 {{ dateTime(data.statement.generated_at) }}。
      </v-alert>
      <div v-if="data?.statement" class="metric-rail" style="--metric-columns: 4">
        <div class="metric-rail__item"><div class="metric-rail__label">实际等效</div><div class="metric-rail__value mono">{{ money(data.statement.actual) }}</div></div>
        <div class="metric-rail__item"><div class="metric-rail__label">人工补录</div><div class="metric-rail__value mono">{{ money(data.statement.manual_actual) }}</div></div>
        <div class="metric-rail__item"><div class="metric-rail__label">梯度计费</div><div class="metric-rail__value mono">{{ money(data.statement.billed) }}</div></div>
        <div class="metric-rail__item"><div class="metric-rail__label">应付</div><div class="metric-rail__value mono">{{ money(data.statement.amount, '¥') }}</div></div>
      </div>
      <div class="two-column">
        <section class="section-band">
          <div class="section-band__head"><div><h2>模型聚合</h2><p>不展示逐 Key 或逐请求数据</p></div></div>
          <div class="section-band__body section-band__body--flush">
            <v-table density="compact">
              <thead><tr><th>模型</th><th class="text-right">请求</th><th class="text-right">Tokens</th><th class="text-right">成本</th></tr></thead>
              <tbody><tr v-for="item in data?.models || []" :key="item.model"><td>{{ item.model }}</td><td class="text-right mono">{{ number(item.requests) }}</td><td class="text-right mono">{{ number(item.tokens) }}</td><td class="text-right mono">{{ money(item.cost) }}</td></tr></tbody>
            </v-table>
          </div>
        </section>
        <section class="section-band">
          <div class="section-band__head"><div><h2>Service Tier</h2><p>请求与等效成本聚合</p></div></div>
          <div class="section-band__body section-band__body--flush">
            <v-table density="compact">
              <thead><tr><th>Tier</th><th class="text-right">请求</th><th class="text-right">成本</th></tr></thead>
              <tbody><tr v-for="item in data?.tiers || []" :key="item.tier"><td>{{ item.tier }}</td><td class="text-right mono">{{ number(item.requests) }}</td><td class="text-right mono">{{ money(item.cost) }}</td></tr></tbody>
            </v-table>
          </div>
        </section>
      </div>
    </LoadingState>
  </div>
</template>
