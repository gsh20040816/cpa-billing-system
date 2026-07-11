<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowUpRight, RefreshCw } from '@lucide/vue'
import { api } from '../api'
import { VChart } from '../charts'
import MetricRail from '../components/MetricRail.vue'
import LoadingState from '../components/LoadingState.vue'
import PageHeader from '../components/PageHeader.vue'
import { dateTime, money, number } from '../lib/format'
import { toQuery } from '../lib/query'

const router = useRouter()
const loading = ref(true)
const error = ref('')
const data = ref(null)
const cycle = ref('')
let refreshTimer = null

const headers = [
  { title: '#', key: 'rank', width: 58, sortable: false },
  { title: 'Telegram 用户', key: 'name', minWidth: 190 },
  { title: '请求', key: 'requests', align: 'end' },
  { title: 'Tokens', key: 'tokens', align: 'end' },
  { title: '实际等效', key: 'actual', align: 'end' },
  { title: '人工补录', key: 'manual_actual', align: 'end' },
  { title: '梯度计费', key: 'billed', align: 'end' },
  { title: '应付', key: 'amount', align: 'end' },
  { title: 'Keys', key: 'key_count', align: 'end' },
]

const rows = computed(() => (data.value?.rows || []).map((item, index) => ({ ...item, rank: index + 1 })))
const metrics = computed(() => {
  const totals = data.value?.totals || {}
  return [
    { label: '请求', value: number(totals.requests), mono: true },
    { label: 'Tokens', value: number(totals.tokens), mono: true },
    { label: '实际等效成本', value: money(totals.actual), mono: true },
    { label: '梯度计费用量', value: money(totals.billed), mono: true },
    { label: '资源池固定成本', value: money(totals.fixed_cost, '¥'), mono: true },
    { label: '按量 Key 应付', value: money(totals.metered_amount, '¥'), mono: true },
    { label: '成员分摊', value: money(totals.member_amount, '¥'), mono: true },
  ]
})

const chartOption = computed(() => {
  const models = data.value?.models || []
  return {
    animationDuration: 350,
    grid: { top: 12, right: 24, bottom: 28, left: 150 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', name: 'USD', splitLine: { lineStyle: { color: '#e4e9e7' } } },
    yAxis: { type: 'category', inverse: true, data: models.map((item) => item.model), axisLabel: { width: 132, overflow: 'truncate' } },
    series: [{ type: 'bar', data: models.map((item) => Number(String(item.cost).replaceAll(',', ''))), itemStyle: { color: '#006c67' }, barMaxWidth: 18 }],
  }
})

async function load(silent = false) {
  if (!silent) loading.value = true
  error.value = ''
  try {
    data.value = await api(`/api/dashboard${toQuery({ cycle: cycle.value })}`)
    if (!cycle.value && data.value?.cycle?.name) cycle.value = data.value.cycle.name
  } catch (exc) {
    error.value = exc.message
  } finally {
    if (!silent) loading.value = false
  }
}

function openUser(item) {
  if (item.telegram_user_id !== null) router.push(`/users/${item.telegram_user_id}${toQuery({ cycle: data.value?.cycle?.name })}`)
}

watch(cycle, (value, previous) => {
  if (previous && value !== previous) load()
})
onMounted(load)
onMounted(() => {
  refreshTimer = window.setInterval(() => {
    if (!document.hidden) load(true)
  }, 15000)
})
onBeforeUnmount(() => window.clearInterval(refreshTimer))
</script>

<template>
  <div class="content-shell">
    <PageHeader
      title="月度成本分摊"
      :subtitle="data?.cycle ? `${data.cycle.start} 至 ${data.cycle.end}` : '尚未创建账期'"
    >
      <template #actions>
        <v-chip v-if="data?.cycle?.estimate_generated_at" variant="tonal" color="secondary">
          实时更新 {{ dateTime(data.cycle.estimate_generated_at) }}
        </v-chip>
        <v-select
          v-if="data?.cycles?.length"
          v-model="cycle"
          :items="data.cycles"
          item-title="name"
          item-value="name"
          label="账期"
          style="min-width: 220px"
        >
          <template #item="{ props, item }">
            <v-list-item v-bind="props" :subtitle="item.raw.status" />
          </template>
        </v-select>
        <v-tooltip text="刷新账务总览">
          <template #activator="{ props }">
            <v-btn v-bind="props" icon variant="outlined" :loading="loading" @click="load"><RefreshCw :size="18" /></v-btn>
          </template>
        </v-tooltip>
      </template>
    </PageHeader>

    <LoadingState :loading="loading" :error="error" :empty="!data?.cycle" empty-text="尚未创建账期" @retry="load">
      <v-alert v-if="data?.cycle?.waiver" type="warning" variant="tonal" border="start" class="mb-4">
        <strong>数据质量说明：</strong>{{ data.cycle.waiver }}
      </v-alert>
      <v-alert v-if="data?.cycle?.unpriced_events" type="warning" variant="tonal" border="start" class="mb-4">
        当前账期仍有 {{ number(data.cycle.unpriced_events) }} 条未计价请求，以下金额是已计价部分的实时估算，暂不能关闭账期。
      </v-alert>
      <MetricRail :items="metrics" :columns="6" />

      <section class="section-band">
        <div class="section-band__head">
          <div><h2>资源池结算进度</h2><p>固定成本先扣除未绑定按量 Key，再分摊给 Telegram 用户</p></div>
          <strong class="mono">合计 {{ money(data?.totals?.amount, '¥') }}</strong>
        </div>
        <div class="section-band__body section-band__body--flush">
          <v-table density="compact">
            <thead><tr><th>资源池</th><th class="text-right">固定成本</th><th class="text-right">按量抵扣</th><th class="text-right">成员剩余成本</th><th class="text-right">成员已分摊</th><th class="text-right">超额按量收入</th></tr></thead>
            <tbody>
              <tr v-for="pool in data?.pool_totals || []" :key="pool.pool_id">
                <td>{{ pool.pool }}</td>
                <td class="text-right mono">{{ money(pool.fixed_cost, '¥') }}</td>
                <td class="text-right mono">{{ money(pool.metered_amount, '¥') }}</td>
                <td class="text-right mono">{{ money(pool.residual_cost, '¥') }}</td>
                <td class="text-right mono">{{ money(pool.member_amount, '¥') }}</td>
                <td class="text-right mono">{{ money((Number(pool.surplus_cents || 0) / 100).toFixed(2), '¥') }}</td>
              </tr>
              <tr v-if="!data?.pool_totals?.length"><td colspan="6" class="text-center data-muted pa-5">当前账期尚未配置资源池成本</td></tr>
            </tbody>
          </v-table>
        </div>
      </section>

      <section v-if="data?.metered_keys?.length" class="section-band">
        <div class="section-band__head"><div><h2>未绑定按量 Key</h2><p>金额 = 本地等效成本 USD × 管理员设置倍率</p></div></div>
        <div class="section-band__body section-band__body--flush">
          <v-data-table
            :headers="[
              { title: 'API Key', key: 'label' },
              { title: '资源池', key: 'pool' },
              { title: '请求', key: 'requests', align: 'end' },
              { title: 'Tokens', key: 'tokens', align: 'end' },
              { title: '等效成本', key: 'actual', align: 'end' },
              { title: '倍率', key: 'multiplier', align: 'end' },
              { title: '预估付费', key: 'amount', align: 'end' },
            ]"
            :items="data.metered_keys.map(item => ({ ...item, label: item.name || item.masked }))"
            :items-per-page="25"
          >
            <template #item.label="{ item }"><span class="mono">{{ item.label }}</span></template>
            <template #item.requests="{ item }"><span class="mono">{{ number(item.requests) }}</span></template>
            <template #item.tokens="{ item }"><span class="mono">{{ number(item.tokens) }}</span></template>
            <template #item.actual="{ item }"><span class="mono">{{ money(item.actual) }}</span></template>
            <template #item.multiplier="{ item }"><span class="mono">{{ item.multiplier }}x</span></template>
            <template #item.amount="{ item }"><span class="mono">{{ money(item.amount, '¥') }}</span></template>
          </v-data-table>
        </div>
      </section>

      <section class="section-band">
        <div class="section-band__head">
          <div><h2>用户排行与账单</h2><p>按 Telegram 用户聚合全部有效 Key</p></div>
        </div>
        <div class="section-band__body section-band__body--flush">
          <v-data-table :headers="headers" :items="rows" :items-per-page="25" hover>
            <template #item.name="{ item }">
              <button v-if="item.telegram_user_id !== null" class="table-link bg-transparent border-0 pa-0 text-left" @click="openUser(item)">
                {{ item.name }} <ArrowUpRight :size="13" class="ml-1" />
              </button>
              <span v-else class="data-muted">{{ item.name }}</span>
            </template>
            <template #item.requests="{ item }"><span class="mono">{{ number(item.requests) }}</span></template>
            <template #item.tokens="{ item }"><span class="mono">{{ number(item.tokens) }}</span></template>
            <template #item.actual="{ item }"><span class="mono">{{ money(item.actual) }}</span></template>
            <template #item.manual_actual="{ item }"><span class="mono">{{ money(item.manual_actual) }}</span></template>
            <template #item.billed="{ item }"><span class="mono">{{ money(item.billed) }}</span></template>
            <template #item.amount="{ item }"><span class="mono">{{ money(item.amount, '¥') }}</span></template>
          </v-data-table>
        </div>
      </section>

      <section class="section-band">
        <div class="section-band__head"><div><h2>模型等效成本</h2><p>当前账期前 20 个模型</p></div></div>
        <div class="section-band__body">
          <VChart v-if="data?.models?.length" class="chart" :option="chartOption" autoresize />
          <div v-else class="pa-8 text-center data-muted">当前账期暂无模型用量</div>
        </div>
      </section>
    </LoadingState>
  </div>
</template>
