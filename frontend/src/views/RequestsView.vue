<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { Filter, RefreshCw, RotateCcw, Search, SlidersHorizontal } from '@lucide/vue'
import { api } from '../api'
import LoadingState from '../components/LoadingState.vue'
import MetricRail from '../components/MetricRail.vue'
import PageHeader from '../components/PageHeader.vue'
import { createDebouncedTask } from '../lib/debounce'
import { dateTime, duration, money, number } from '../lib/format'
import { activeFilterCount, toQuery } from '../lib/query'

const props = defineProps({
  admin: { type: Boolean, default: false },
})
const endpointBase = props.admin ? '/api/admin/usage' : '/api/me/usage'
const apiOptions = props.admin ? { admin: true } : {}
const loading = ref(true)
const optionsLoading = ref(true)
const error = ref('')
const options = ref({ models: [], tiers: [], providers: [], failure_codes: [], keys: [] })
const data = ref(null)
const filtersOpen = ref(true)
const detail = ref(null)
let requestSequence = 0

const defaults = {
  start: '', end: '', model: [], tier: '', provider: '', status: 'all', key_id: '', failure_code: '',
  min_tokens: '', max_tokens: '', min_cost: '', max_cost: '', min_latency: '', max_latency: '',
  min_ttft: '', max_ttft: '', min_tps: '', max_tps: '', long_context: '', q: '', sort: 'time_desc', page: 1, page_size: 50,
}
const filters = reactive({ ...defaults })

const statusItems = [
  { title: '全部', value: 'all' },
  { title: '成功', value: 'success' },
  { title: '失败', value: 'failed' },
  { title: '已计价', value: 'priced' },
  { title: '未计价', value: 'unpriced' },
]
const sortItems = [
  { title: '时间从新到旧', value: 'time_desc' },
  { title: '时间从旧到新', value: 'time_asc' },
  { title: 'Tokens 从高到低', value: 'tokens_desc' },
  { title: '成本从高到低', value: 'cost_desc' },
  { title: '延迟从高到低', value: 'latency_desc' },
  { title: 'TTFT 从高到低', value: 'ttft_desc' },
  { title: '真实 TPS 从高到低', value: 'tps_desc' },
]
const longContextItems = [
  { title: '全部上下文', value: '' },
  { title: '长上下文', value: true },
  { title: '普通上下文', value: false },
]
const headers = [
  { title: '时间', key: 'occurred_at', minWidth: 168 },
  { title: 'Key', key: 'key', minWidth: 145 },
  ...(props.admin ? [{ title: '历史归属', key: 'owner', minWidth: 145 }] : []),
  { title: '模型', key: 'model', minWidth: 150 },
  { title: 'Tier', key: 'service_tier', width: 92 },
  { title: '推理强度', key: 'reasoning_effort', width: 108 },
  { title: 'Input', key: 'input_tokens', align: 'end' },
  { title: '缓存读取', key: 'cache_read', align: 'end' },
  { title: 'Output', key: 'output_tokens', align: 'end' },
  { title: '真实 TPS', key: 'tps', align: 'end' },
  { title: 'TTFT', key: 'ttft_ms', align: 'end' },
  { title: '延迟', key: 'latency_ms', align: 'end' },
  { title: '成本', key: 'cost', align: 'end' },
  { title: '状态', key: 'status', width: 94 },
]

const filterCount = computed(() => activeFilterCount(filters, ['page', 'page_size', 'sort']))
const detailOpen = computed({
  get: () => detail.value !== null,
  set: (open) => {
    if (!open) detail.value = null
  },
})
const metrics = computed(() => {
  const summary = data.value?.summary || {}
  return [
    { label: '筛选后请求', value: number(summary.requests), mono: true },
    { label: 'Input Tokens', value: number(summary.input_tokens), mono: true },
    { label: 'Output Tokens', value: number(summary.output_tokens), mono: true },
    { label: '等效成本', value: money(summary.cost), mono: true },
    { label: '失败', value: number(summary.failed), mono: true },
    { label: '未计价', value: number(summary.unpriced), mono: true },
  ]
})

function requestParams() {
  return {
    ...filters,
    key_id: filters.key_id || null,
    failure_code: filters.failure_code === '' ? null : filters.failure_code,
    long_context: filters.long_context === '' ? null : filters.long_context,
  }
}

const filterSignature = computed(() => {
  const { page: _page, ...params } = requestParams()
  return JSON.stringify(params)
})

async function loadOptions() {
  optionsLoading.value = true
  try {
    options.value = await api(`${endpointBase}/filter-options`, apiOptions)
  } finally {
    optionsLoading.value = false
  }
}

async function load() {
  const sequence = ++requestSequence
  loading.value = true
  error.value = ''
  try {
    const result = await api(`${endpointBase}/events${toQuery(requestParams())}`, apiOptions)
    if (sequence === requestSequence) data.value = result
  } catch (exc) {
    if (sequence === requestSequence) error.value = exc.message
  } finally {
    if (sequence === requestSequence) loading.value = false
  }
}

function reloadFromFirstPage() {
  if (filters.page !== 1) {
    filters.page = 1
    return
  }
  return load()
}

const autoReload = createDebouncedTask(reloadFromFirstPage, 300)

function refreshCurrentPage() {
  autoReload.cancel()
  return load()
}

function refreshFromFirstPage() {
  return autoReload.run()
}

function resetFilters() {
  const { page: _page, ...filterDefaults } = defaults
  Object.assign(filters, filterDefaults)
}

function keyLabel(item) {
  return item.name ? `${item.name} · ${item.masked}` : item.masked
}

function ownerLabel(owner) {
  return owner?.name || (owner?.telegram_user_id ? `TG ${owner.telegram_user_id}` : '未绑定')
}

onMounted(async () => {
  await Promise.all([loadOptions(), load()])
})
watch(filterSignature, () => autoReload.schedule())
watch(() => filters.page, () => load())
onBeforeUnmount(autoReload.cancel)
</script>

<template>
  <div class="content-shell">
    <PageHeader
      :title="props.admin ? '全部请求' : '历史请求'"
      :subtitle="props.admin ? '全站所有 API Key 的完整请求元数据，包含未绑定 Key' : '本人所有 API Key 的完整请求元数据'"
    >
      <template #actions>
        <v-btn variant="outlined" @click="filtersOpen = !filtersOpen">
          <SlidersHorizontal :size="17" class="mr-2" />筛选
          <v-badge v-if="filterCount" :content="filterCount" color="secondary" inline class="ml-2" />
        </v-btn>
        <v-tooltip text="刷新请求列表">
          <template #activator="{ props }">
            <v-btn v-bind="props" icon variant="outlined" :loading="loading" @click="refreshCurrentPage"><RefreshCw :size="18" /></v-btn>
          </template>
        </v-tooltip>
      </template>
    </PageHeader>

    <v-expand-transition>
      <section v-show="filtersOpen" class="section-band mb-4">
        <div class="section-band__head">
          <div class="d-flex align-center ga-2"><Filter :size="18" /><h2>请求筛选</h2></div>
          <v-btn size="small" variant="text" @click="resetFilters"><RotateCcw :size="16" class="mr-2" />重置</v-btn>
        </div>
        <div class="section-band__body">
          <div class="filter-grid filter-grid--six">
            <v-text-field v-model="filters.q" label="Request ID / 模型" clearable @keyup.enter="refreshFromFirstPage">
              <template #prepend-inner><Search :size="16" /></template>
            </v-text-field>
            <v-text-field v-model="filters.start" label="开始时间" type="datetime-local" />
            <v-text-field v-model="filters.end" label="结束时间" type="datetime-local" />
            <v-autocomplete v-model="filters.model" :items="options.models" label="模型" multiple chips clearable :loading="optionsLoading" />
            <v-select v-model="filters.tier" :items="options.tiers" label="Service Tier" clearable />
            <v-select v-model="filters.provider" :items="options.providers" label="Provider" clearable />
            <v-select v-model="filters.key_id" :items="options.keys" :item-title="keyLabel" item-value="id" label="API Key" clearable />
            <v-select v-model="filters.failure_code" :items="options.failure_codes" label="失败状态码" clearable />
            <v-select v-model="filters.long_context" :items="longContextItems" label="上下文类型" />
            <v-text-field v-model="filters.min_tokens" label="最小 Tokens" type="number" min="0" />
            <v-text-field v-model="filters.max_tokens" label="最大 Tokens" type="number" min="0" />
            <v-text-field v-model="filters.min_cost" label="最小成本 USD" type="number" min="0" step="0.0001" />
            <v-text-field v-model="filters.max_cost" label="最大成本 USD" type="number" min="0" step="0.0001" />
            <v-text-field v-model="filters.min_ttft" label="最小 TTFT ms" type="number" min="0" />
            <v-text-field v-model="filters.max_ttft" label="最大 TTFT ms" type="number" min="0" />
            <v-text-field v-model="filters.min_latency" label="最小延迟 ms" type="number" min="0" />
            <v-text-field v-model="filters.max_latency" label="最大延迟 ms" type="number" min="0" />
            <v-text-field v-model="filters.min_tps" label="最小真实 TPS" type="number" min="0" step="0.01" />
            <v-text-field v-model="filters.max_tps" label="最大真实 TPS" type="number" min="0" step="0.01" />
            <v-select v-model="filters.sort" :items="sortItems" label="排序" />
          </div>
          <div class="d-flex align-center justify-space-between ga-4 flex-wrap mt-4">
            <v-btn-toggle v-model="filters.status" color="primary" mandatory density="compact" divided>
              <v-btn v-for="item in statusItems" :key="item.value" :value="item.value" size="small">{{ item.title }}</v-btn>
            </v-btn-toggle>
            <div class="filter-actions mt-0">
              <v-select v-model="filters.page_size" :items="[25, 50, 100]" label="每页" style="width: 110px" />
              <v-progress-circular v-if="loading && data" indeterminate color="primary" size="20" width="2" aria-label="正在更新请求" />
            </div>
          </div>
        </div>
      </section>
    </v-expand-transition>

    <MetricRail v-if="data" :items="metrics" :columns="6" />

    <section class="section-band">
      <div class="section-band__head">
        <div><h2>{{ props.admin ? '全站请求明细' : '请求明细' }}</h2><p>不包含提示词或响应正文</p></div>
        <span v-if="data" class="data-muted text-body-2">共 {{ number(data.pagination.total) }} 条</span>
      </div>
      <div class="section-band__body section-band__body--flush">
        <LoadingState :loading="loading && !data" :error="error" :empty="!data?.items?.length" empty-text="当前筛选条件下没有请求" @retry="refreshCurrentPage">
          <v-data-table :headers="headers" :items="data?.items || []" :items-per-page="-1" :loading="loading" hide-default-footer hover @click:row="(_, row) => detail = row.item">
            <template #item.occurred_at="{ item }"><span class="nowrap">{{ dateTime(item.occurred_at) }}</span></template>
            <template #item.key="{ item }"><span class="mono">{{ item.key.name || item.key.masked }}</span></template>
            <template #item.owner="{ item }">
              <v-chip :color="item.owner ? 'secondary' : 'warning'" variant="tonal">{{ ownerLabel(item.owner) }}</v-chip>
            </template>
            <template #item.model="{ item }">
              <div>{{ item.resolved_model || item.model }}</div>
              <div v-if="item.requested_model && item.requested_model !== item.resolved_model" class="data-muted text-caption">请求 {{ item.requested_model }}</div>
            </template>
            <template #item.service_tier="{ item }"><v-chip variant="tonal" color="secondary">{{ item.service_tier }}</v-chip></template>
            <template #item.reasoning_effort="{ item }"><v-chip v-if="item.reasoning_effort" variant="tonal" color="secondary">{{ item.reasoning_effort }}</v-chip><span v-else class="data-muted">-</span></template>
            <template #item.input_tokens="{ item }"><span class="mono">{{ number(item.tokens.input) }}</span></template>
            <template #item.cache_read="{ item }"><span class="mono">{{ number(item.tokens.cache_read) }}</span></template>
            <template #item.output_tokens="{ item }"><span class="mono">{{ number(item.tokens.output) }}</span></template>
            <template #item.tps="{ item }"><span class="mono">{{ item.tps === null ? '-' : Number(item.tps).toFixed(2) }}</span></template>
            <template #item.ttft_ms="{ item }"><span class="mono">{{ duration(item.ttft_ms) }}</span></template>
            <template #item.latency_ms="{ item }"><span class="mono">{{ duration(item.latency_ms) }}</span></template>
            <template #item.cost="{ item }"><span class="mono">{{ item.cost === null ? '未计价' : money(item.cost) }}</span></template>
            <template #item.status="{ item }">
              <v-chip :color="item.failed ? 'error' : item.pricing_status === 'unpriced' ? 'warning' : 'success'" variant="tonal">
                {{ item.failed ? `失败${item.status_code ? ` ${item.status_code}` : ''}` : '成功' }}
              </v-chip>
            </template>
          </v-data-table>
          <div class="d-flex justify-center pa-4 border-t">
            <v-pagination v-model="filters.page" :length="data?.pagination?.total_pages || 1" :total-visible="7" />
          </div>
        </LoadingState>
      </div>
    </section>

    <v-dialog v-model="detailOpen" max-width="760">
      <v-card v-if="detail">
        <v-card-title>请求 {{ detail.request_id || `#${detail.id}` }}</v-card-title>
        <v-card-text>
          <div class="detail-grid">
            <div><span>时间</span><strong>{{ dateTime(detail.occurred_at) }}</strong></div>
            <div><span>API Key</span><strong class="mono">{{ detail.key.name || detail.key.masked }}</strong></div>
            <div v-if="props.admin"><span>历史归属</span><strong>{{ ownerLabel(detail.owner) }}</strong></div>
            <div><span>模型</span><strong>{{ detail.resolved_model || detail.model }}</strong></div>
            <div><span>Tier</span><strong>{{ detail.service_tier }}</strong></div>
            <div><span>推理强度</span><strong class="mono">{{ detail.reasoning_effort || '-' }}</strong></div>
            <div><span>Input</span><strong class="mono">{{ number(detail.tokens.input) }}</strong></div>
            <div><span>Output</span><strong class="mono">{{ number(detail.tokens.output) }}</strong></div>
            <div><span>缓存读取</span><strong class="mono">{{ number(detail.tokens.cache_read) }}</strong></div>
            <div><span>缓存创建</span><strong class="mono">{{ number(detail.tokens.cache_creation) }}</strong></div>
            <div><span>Reasoning</span><strong class="mono">{{ number(detail.tokens.reasoning) }}</strong></div>
            <div><span>TTFT</span><strong class="mono">{{ duration(detail.ttft_ms) }}</strong></div>
            <div><span>延迟</span><strong class="mono">{{ duration(detail.latency_ms) }}</strong></div>
            <div><span>生成阶段</span><strong class="mono">{{ duration(detail.generation_ms) }}</strong></div>
            <div><span>真实 TPS</span><strong class="mono">{{ detail.tps === null ? '-' : Number(detail.tps).toFixed(2) }}</strong></div>
            <div><span>总 Tokens</span><strong class="mono">{{ number(detail.tokens.total) }}</strong></div>
            <div><span>等效成本</span><strong class="mono">{{ detail.cost === null ? '未计价' : money(detail.cost) }}</strong></div>
          </div>
        </v-card-text>
        <v-card-actions><v-spacer /><v-btn variant="text" @click="detail = null">关闭</v-btn></v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<style scoped>
.detail-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1px; background: #dfe5e2; border: 1px solid #dfe5e2; }
.detail-grid > div { min-width: 0; padding: 12px; background: #fff; }
.detail-grid span { display: block; color: #68716e; font-size: 0.72rem; margin-bottom: 5px; }
.detail-grid strong { display: block; overflow-wrap: anywhere; }
@media (max-width: 700px) { .detail-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
