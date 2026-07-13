<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { RefreshCw } from '@lucide/vue'
import { api } from '../api'
import { VChart } from '../charts'
import LoadingState from '../components/LoadingState.vue'
import MetricRail from '../components/MetricRail.vue'
import PageHeader from '../components/PageHeader.vue'
import TimeRangeSelector from '../components/TimeRangeSelector.vue'
import { useAutoRefresh } from '../lib/autoRefresh'
import { createDebouncedTask } from '../lib/debounce'
import { compactNumber, dateTime, duration, money, number, percent } from '../lib/format'
import { toQuery } from '../lib/query'
import { DEFAULT_TIME_RANGE, isTimeRangeReady, timeRangeQuery } from '../lib/timeRange'

const loading = ref(true)
const error = ref('')
const data = ref(null)
const filters = reactive({ ...DEFAULT_TIME_RANGE, window: '60m' })
const timeRangeModel = computed({
  get: () => filters,
  set: (value) => Object.assign(filters, value),
})
let requestSequence = 0

const overview = computed(() => data.value?.keeper?.overview || {})
const realtime = computed(() => data.value?.keeper?.realtime || {})
const summary = computed(() => overview.value.summary || {})
const serviceHealth = computed(() => overview.value.service_health || {})
const metrics = computed(() => [
  { label: '请求', value: number(summary.value.request_count), mono: true },
  { label: 'Tokens', value: compactNumber(summary.value.token_count), hint: number(summary.value.token_count), mono: true },
  { label: 'RPM', value: Number(summary.value.rpm || 0).toFixed(2), mono: true },
  { label: 'TPM', value: compactNumber(summary.value.tpm), mono: true },
  { label: '本地等效成本', value: summary.value.cost_available ? money(summary.value.total_cost) : '-', mono: true },
  { label: '成功率', value: percent(serviceHealth.value.success_rate, 2), mono: true },
])

const tokenOption = computed(() => ({
  animationDuration: 300,
  grid: { top: 24, right: 24, bottom: 42, left: 74 },
  tooltip: { trigger: 'axis' },
  xAxis: { type: 'category', data: (realtime.value.token_velocity || []).map((item) => dateTime(item.bucket)), axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value', name: 'Tokens/min', splitLine: { lineStyle: { color: '#e4e9e7' } } },
  series: [{ name: 'Tokens/min', type: 'line', showSymbol: false, smooth: 0.18, data: (realtime.value.token_velocity || []).map((item) => item.tokens_per_minute), lineStyle: { color: '#006c67', width: 2 }, areaStyle: { color: 'rgba(0,108,103,.08)' } }],
}))

const responseOption = computed(() => {
  const rows = realtime.value.response_level || []
  return {
    animationDuration: 300,
    grid: { top: 34, right: 24, bottom: 42, left: 70 },
    tooltip: { trigger: 'axis', valueFormatter: (value) => `${Number(value).toFixed(0)} ms` },
    legend: { top: 0 },
    xAxis: { type: 'category', data: rows.map((item) => dateTime(item.bucket)), axisLabel: { hideOverlap: true } },
    yAxis: { type: 'value', name: 'ms', splitLine: { lineStyle: { color: '#e4e9e7' } } },
    series: [
      { name: 'TTFT P50', type: 'line', showSymbol: false, data: rows.map((item) => item.ttft_p50_ms), lineStyle: { color: '#006c67' } },
      { name: 'TTFT P95', type: 'line', showSymbol: false, data: rows.map((item) => item.ttft_p95_ms), lineStyle: { color: '#315ea8' } },
      { name: 'Latency P50', type: 'line', showSymbol: false, data: rows.map((item) => item.latency_p50_ms), lineStyle: { color: '#a85d00' } },
      { name: 'Latency P95', type: 'line', showSymbol: false, data: rows.map((item) => item.latency_p95_ms), lineStyle: { color: '#b3261e' } },
    ],
  }
})

function healthTone(item) {
  const rate = healthRate(item)
  if (rate < 0) return ''
  if (rate >= 99) return 'health-cell--ok'
  if (rate >= 90) return 'health-cell--warn'
  return 'health-cell--error'
}

function healthRate(item) {
  const raw = Number(item?.rate ?? -1)
  if (raw < 0) return raw
  if (serviceHealth.value.source !== 'billing-panel' && raw <= 1) return raw * 100
  return raw
}

function statusParams() {
  return {
    ...timeRangeQuery(filters),
    window: filters.window,
  }
}

async function load(silent = false) {
  if (!isTimeRangeReady(filters)) return
  const sequence = ++requestSequence
  if (!silent) loading.value = true
  error.value = ''
  try {
    const result = await api(`/api/site/status${toQuery(statusParams())}`)
    if (sequence === requestSequence) data.value = result
  } catch (exc) {
    if (sequence === requestSequence) error.value = exc.message
  } finally {
    if (sequence === requestSequence && !silent) loading.value = false
  }
}

const autoRefresh = useAutoRefresh((silent) => load(silent))
const filterReload = createDebouncedTask(() => autoRefresh.refresh(), 180)

watch(
  () => [filters.range, filters.cycle, filters.custom_hours, filters.window],
  () => filterReload.schedule(),
)

onBeforeUnmount(filterReload.cancel)
</script>

<template>
  <div class="content-shell">
    <PageHeader title="全站状态" subtitle="请求、Token、费用与性能均由本面板计算；Keeper 仅提供运行和额度状态">
      <template #actions>
        <v-select v-model="filters.window" :items="['15m', '30m', '45m', '60m']" label="实时窗口" style="width: 130px" />
        <v-tooltip text="刷新全站状态">
          <template #activator="{ props }"><v-btn v-bind="props" icon variant="outlined" :loading="loading" @click="autoRefresh.refresh()"><RefreshCw :size="18" /></v-btn></template>
        </v-tooltip>
      </template>
    </PageHeader>

    <div class="d-flex align-center ga-3 flex-wrap mb-4">
      <TimeRangeSelector v-model="timeRangeModel" />
      <v-progress-circular v-if="loading && data" indeterminate color="primary" size="20" width="2" aria-label="正在更新状态" />
    </div>

    <LoadingState :loading="loading && !data" :error="error" :empty="!data" @retry="autoRefresh.refresh()">
      <v-alert v-if="data?.degraded" type="warning" variant="tonal" border="start" class="mb-4">
        部分状态源不可用：{{ data.errors.join('、') }}
      </v-alert>
      <v-alert v-if="summary.unpriced_events" type="warning" variant="tonal" border="start" class="mb-4">
        当前范围有 {{ number(summary.unpriced_events) }} 条未计价请求，费用仅包含已匹配价格的事件。
      </v-alert>
      <MetricRail :items="metrics" :columns="6" />

      <section class="section-band">
        <div class="section-band__head">
          <div><h2>运行状态</h2><p>最近刷新 {{ dateTime(data?.generated_at) }}</p></div>
          <div class="status-inline">
            <span class="status-dot" :class="data?.cpa?.reachable ? 'status-dot--ok' : 'status-dot--error'" />
            <span>CPA {{ data?.cpa?.reachable ? `${data.cpa.latency_ms} ms` : '不可用' }}</span>
          </div>
        </div>
        <div class="section-band__body">
          <div class="three-column">
            <div class="status-panel">
              <span>CPA 管理接口</span><strong>{{ data?.cpa?.reachable ? '正常' : '异常' }}</strong>
              <small>{{ data?.cpa?.reachable ? `${number(data.cpa.api_key_count)} 个有效 Key` : data?.cpa?.error }}</small>
            </div>
            <div class="status-panel">
              <span>Keeper</span><strong>{{ data?.keeper?.available && data.keeper.status?.running ? '运行中' : '异常' }}</strong>
              <small>{{ data?.keeper?.version?.version || '-' }} · {{ data?.keeper?.update?.updateAvailable ? `可更新 ${data.keeper.update.latestVersion || ''}` : '已是当前版本' }}</small>
            </div>
            <div class="status-panel">
              <span>Billing Worker</span><strong>{{ data?.billing?.sync?.every?.(item => !item.last_error) ? '同步正常' : '需要检查' }}</strong>
              <small>Backlog {{ number(data?.billing?.sync?.reduce?.((sum, item) => sum + Number(item.backlog || 0), 0)) }}</small>
            </div>
          </div>
        </div>
      </section>

      <div class="two-column mt-4">
        <section class="section-band">
          <div class="section-band__head"><div><h2>Token 速度</h2><p>{{ realtime.window || filters.window }} 实时窗口</p></div></div>
          <div class="section-band__body"><VChart class="chart" :option="tokenOption" autoresize /></div>
        </section>
        <section class="section-band">
          <div class="section-band__head"><div><h2>响应时间</h2><p>TTFT 与完整延迟分位数</p></div></div>
          <div class="section-band__body"><VChart class="chart" :option="responseOption" autoresize /></div>
        </section>
      </div>

      <section class="section-band">
        <div class="section-band__head">
          <div><h2>服务健康轨道</h2><p>{{ number(serviceHealth.total_success) }} 成功 · {{ number(serviceHealth.total_failure) }} 失败</p></div>
          <strong class="mono">{{ percent(serviceHealth.success_rate, 3) }}</strong>
        </div>
        <div class="section-band__body">
          <div class="health-grid">
            <v-tooltip v-for="(item, index) in serviceHealth.block_details || []" :key="index" content-class="health-tooltip" :text="`${dateTime(item.start_time)} · 成功 ${item.success} · 失败 ${item.failure} · 成功率 ${healthRate(item) < 0 ? '-' : `${healthRate(item).toFixed(2)}%`}`">
              <template #activator="{ props }"><span v-bind="props" class="health-cell" :class="healthTone(item)" /></template>
            </v-tooltip>
          </div>
        </div>
      </section>

      <div class="two-column mt-4">
        <section class="section-band">
          <div class="section-band__head"><div><h2>当前模型</h2><p>实时窗口内聚合</p></div></div>
          <div class="section-band__body section-band__body--flush">
            <v-table density="compact">
              <thead><tr><th>模型</th><th class="text-right">请求</th><th class="text-right">Tokens</th><th class="text-right">成本</th></tr></thead>
              <tbody><tr v-for="item in realtime.current_usage?.models || []" :key="item.label"><td>{{ item.label }}</td><td class="text-right mono">{{ number(item.requests) }}</td><td class="text-right mono">{{ number(item.tokens) }}</td><td class="text-right mono">{{ money(item.cost) }}</td></tr></tbody>
            </v-table>
          </div>
        </section>
        <section class="section-band">
          <div class="section-band__head"><div><h2>实时响应分布</h2><p>当前窗口聚合统计</p></div></div>
          <div class="section-band__body">
            <dl class="response-stats">
              <div><dt>最新 TTFT P50</dt><dd>{{ duration(realtime.response_level?.at?.(-1)?.ttft_p50_ms) }}</dd></div>
              <div><dt>最新 TTFT P95</dt><dd>{{ duration(realtime.response_level?.at?.(-1)?.ttft_p95_ms) }}</dd></div>
              <div><dt>最新 Latency P50</dt><dd>{{ duration(realtime.response_level?.at?.(-1)?.latency_p50_ms) }}</dd></div>
              <div><dt>最新 Latency P95</dt><dd>{{ duration(realtime.response_level?.at?.(-1)?.latency_p95_ms) }}</dd></div>
              <div><dt>活跃 API Keys</dt><dd>{{ number(realtime.current_usage?.api_keys?.count) }}</dd></div>
              <div><dt>活跃上游账号</dt><dd>{{ number(realtime.current_usage?.upstream_accounts?.length) }}</dd></div>
            </dl>
          </div>
        </section>
      </div>

      <section class="section-band">
        <div class="section-band__head"><div><h2>同步与对账</h2><p>CPAMP 只读镜像状态</p></div></div>
        <div class="section-band__body section-band__body--flush">
          <v-table density="compact">
            <thead><tr><th>来源</th><th>Last ID</th><th>最后事件</th><th>最后成功</th><th class="text-right">Backlog</th><th>错误</th></tr></thead>
            <tbody><tr v-for="item in data?.billing?.sync || []" :key="item.source"><td>{{ item.source }}</td><td class="mono">{{ number(item.last_event_id) }}</td><td>{{ dateTime(item.last_event_at) }}</td><td>{{ dateTime(item.last_success_at) }}</td><td class="text-right mono">{{ number(item.backlog) }}</td><td :class="item.last_error ? 'data-error' : 'data-muted'">{{ item.last_error || '-' }}</td></tr></tbody>
          </v-table>
        </div>
      </section>
    </LoadingState>
  </div>
</template>

<style scoped>
.status-panel { padding: 14px 16px; border-left: 3px solid #006c67; background: #f6f8f7; }
.status-panel span, .status-panel small { display: block; color: #66706d; }
.status-panel strong { display: block; margin: 5px 0; font-size: 1.1rem; }
.response-stats { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); margin: 0; border: 1px solid #e1e7e4; }
.response-stats > div { padding: 13px; border-right: 1px solid #e1e7e4; border-bottom: 1px solid #e1e7e4; }
.response-stats > div:nth-child(2n) { border-right: 0; }
.response-stats dt { color: #66706d; font-size: .74rem; }
.response-stats dd { margin: 5px 0 0; font-family: 'JetBrains Mono', monospace; font-weight: 700; }
</style>
