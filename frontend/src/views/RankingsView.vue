<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { RefreshCw } from '@lucide/vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import { VChart } from '../charts'
import LoadingState from '../components/LoadingState.vue'
import MetricRail from '../components/MetricRail.vue'
import PageHeader from '../components/PageHeader.vue'
import { money, number, percent } from '../lib/format'
import { toQuery } from '../lib/query'

const router = useRouter()
const loading = ref(true)
const error = ref('')
const data = ref(null)
const filters = reactive({ range: '24h', start: '', end: '', cycle: '', sort: 'cost' })
const ranges = [
  { title: '24h', value: '24h' },
  { title: '7d', value: '7d' },
  { title: '30d', value: '30d' },
  { title: '当前账期', value: 'cycle' },
  { title: '全部', value: 'all' },
  { title: '自定义', value: 'custom' },
]
const sorts = [
  { title: '等效成本', value: 'cost' },
  { title: 'Tokens', value: 'tokens' },
  { title: '请求', value: 'requests' },
  { title: '失败', value: 'failures' },
]
const headers = [
  { title: '#', key: 'rank', width: 56 },
  { title: 'Telegram 用户', key: 'name', minWidth: 190 },
  { title: '请求', key: 'requests', align: 'end' },
  { title: 'Tokens', key: 'tokens', align: 'end' },
  { title: '等效成本', key: 'cost', align: 'end' },
  { title: '失败', key: 'failed', align: 'end' },
  { title: '成功率', key: 'success_rate', align: 'end' },
  { title: '长上下文', key: 'long_context', align: 'end' },
  { title: 'Keys', key: 'key_count', align: 'end' },
]

const rows = computed(() => (data.value?.rows || []).map((item, index) => ({ ...item, rank: index + 1 })))
const metrics = computed(() => [
  { label: '请求', value: number(data.value?.totals?.requests), mono: true },
  { label: 'Tokens', value: number(data.value?.totals?.tokens), mono: true },
  { label: '等效成本', value: money(data.value?.totals?.cost), mono: true },
  { label: '失败', value: number(data.value?.totals?.failed), mono: true },
  { label: '排行用户项', value: number(data.value?.rows?.length), mono: true },
])

const chartOption = computed(() => {
  const top = rows.value.slice(0, 12).reverse()
  const valueKey = filters.sort === 'failures' ? 'failed' : filters.sort === 'cost' ? 'cost_nano_usd' : filters.sort
  return {
    animationDuration: 350,
    grid: { top: 16, right: 28, bottom: 32, left: 150 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', splitLine: { lineStyle: { color: '#e4e9e7' } } },
    yAxis: { type: 'category', data: top.map((item) => item.name), axisLabel: { width: 135, overflow: 'truncate' } },
    series: [{ type: 'bar', data: top.map((item) => item[valueKey]), itemStyle: { color: '#315ea8' }, barMaxWidth: 18 }],
  }
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api(`/api/rankings${toQuery(filters)}`)
  } catch (exc) {
    error.value = exc.message
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="content-shell">
    <PageHeader title="用量排行" :subtitle="data?.range ? `${data.range.start || '最早记录'} 至 ${data.range.end}` : '按 Telegram 用户聚合'">
      <template #actions>
        <v-select v-model="filters.sort" :items="sorts" label="排序指标" style="width: 160px" />
        <v-tooltip text="刷新排行">
          <template #activator="{ props }"><v-btn v-bind="props" icon variant="outlined" :loading="loading" @click="load"><RefreshCw :size="18" /></v-btn></template>
        </v-tooltip>
      </template>
    </PageHeader>

    <div class="d-flex align-center ga-3 flex-wrap mb-4">
      <v-btn-toggle v-model="filters.range" mandatory divided color="primary" density="compact">
        <v-btn v-for="item in ranges" :key="item.value" :value="item.value" size="small">{{ item.title }}</v-btn>
      </v-btn-toggle>
      <template v-if="filters.range === 'custom'">
        <v-text-field v-model="filters.start" type="date" label="开始" style="max-width: 180px" />
        <v-text-field v-model="filters.end" type="date" label="结束" style="max-width: 180px" />
      </template>
      <v-btn color="primary" size="small" @click="load">应用</v-btn>
    </div>

    <LoadingState :loading="loading" :error="error" :empty="!data" @retry="load">
      <MetricRail :items="metrics" :columns="5" />
      <section class="section-band">
        <div class="section-band__head"><div><h2>排行分布</h2><p>前 12 个聚合项</p></div></div>
        <div class="section-band__body"><VChart class="chart" :option="chartOption" autoresize /></div>
      </section>
      <section class="section-band">
        <div class="section-band__head"><div><h2>Telegram 用户与未绑定 Key</h2><p>已绑定 Key 按 Telegram 用户聚合；未绑定 Key 分别以别名或掩码显示</p></div></div>
        <div class="section-band__body section-band__body--flush">
          <v-data-table :headers="headers" :items="rows" :items-per-page="50" hover>
            <template #item.name="{ item }">
              <button v-if="item.telegram_user_id !== null" class="table-link bg-transparent border-0 pa-0" @click="router.push(`/users/${item.telegram_user_id}`)">{{ item.name }}</button>
              <span v-else class="data-muted">{{ item.name }}</span>
            </template>
            <template #item.requests="{ item }"><span class="mono">{{ number(item.requests) }}</span></template>
            <template #item.tokens="{ item }"><span class="mono">{{ number(item.tokens) }}</span></template>
            <template #item.cost="{ item }"><span class="mono">{{ money(item.cost) }}</span></template>
            <template #item.failed="{ item }"><span class="mono" :class="item.failed ? 'data-error' : ''">{{ number(item.failed) }}</span></template>
            <template #item.success_rate="{ item }"><span class="mono">{{ percent(item.success_rate, 2) }}</span></template>
            <template #item.long_context="{ item }"><span class="mono">{{ number(item.long_context) }}</span></template>
          </v-data-table>
        </div>
      </section>
    </LoadingState>
  </div>
</template>
