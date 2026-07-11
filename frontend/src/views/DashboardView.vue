<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowUpRight, RefreshCw } from '@lucide/vue'
import { api } from '../api'
import { VChart } from '../charts'
import MetricRail from '../components/MetricRail.vue'
import LoadingState from '../components/LoadingState.vue'
import PageHeader from '../components/PageHeader.vue'
import { money, number } from '../lib/format'
import { toQuery } from '../lib/query'

const router = useRouter()
const loading = ref(true)
const error = ref('')
const data = ref(null)
const cycle = ref('')

const headers = [
  { title: '#', key: 'rank', width: 58, sortable: false },
  { title: 'Telegram 用户', key: 'name', minWidth: 190 },
  { title: '请求', key: 'requests', align: 'end' },
  { title: 'Tokens', key: 'tokens', align: 'end' },
  { title: '实际等效', key: 'actual', align: 'end' },
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
    { label: '分摊总额', value: money(totals.amount, '¥'), mono: true },
    { label: '用户与未绑定项', value: number(data.value?.rows?.length), mono: true },
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

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api(`/api/dashboard${toQuery({ cycle: cycle.value })}`)
    if (!cycle.value && data.value?.cycle?.name) cycle.value = data.value.cycle.name
  } catch (exc) {
    error.value = exc.message
  } finally {
    loading.value = false
  }
}

function openUser(item) {
  if (item.telegram_user_id !== null) router.push(`/users/${item.telegram_user_id}${toQuery({ cycle: data.value?.cycle?.name })}`)
}

watch(cycle, (value, previous) => {
  if (previous && value !== previous) load()
})
onMounted(load)
</script>

<template>
  <div class="content-shell">
    <PageHeader
      title="月度成本分摊"
      :subtitle="data?.cycle ? `${data.cycle.start} 至 ${data.cycle.end}` : '尚未创建账期'"
    >
      <template #actions>
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
      <MetricRail :items="metrics" :columns="6" />

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
