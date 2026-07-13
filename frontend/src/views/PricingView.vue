<script setup>
import { computed, ref, watch } from 'vue'
import { RefreshCw, Search } from '@lucide/vue'
import { api } from '../api'
import LoadingState from '../components/LoadingState.vue'
import PageHeader from '../components/PageHeader.vue'
import { useAutoRefresh } from '../lib/autoRefresh'
import { dateTime, money, number } from '../lib/format'
import { effectiveRate, priceSourceText } from '../lib/pricing'
import { toQuery } from '../lib/query'

const loading = ref(true)
const error = ref('')
const data = ref(null)
const cycle = ref('')
const search = ref('')
const tier = ref('default')
const tiers = [
  { title: 'Default', value: 'default' },
  { title: 'Priority / Fast', value: 'priority' },
  { title: 'Flex', value: 'flex' },
]

const modelHeaders = [
  { title: '模型', key: 'model', minWidth: 190 },
  { title: 'Input / 1M', key: 'input', align: 'end' },
  { title: 'Cache read / 1M', key: 'cache_read', align: 'end' },
  { title: 'Cache creation / 1M', key: 'cache_creation', align: 'end' },
  { title: 'Output / 1M', key: 'output', align: 'end' },
  { title: '价格来源', key: 'price_source' },
  { title: '长上下文阈值', key: 'threshold', align: 'end' },
  { title: '长上下文倍率', key: 'multipliers', align: 'end' },
]

function rateText(item, field) {
  const rate = effectiveRate(item, field, tier.value)
  return rate ? money(rate.usd_per_million) : '-'
}

const models = computed(() => {
  const needle = search.value.trim().toLowerCase()
  return (data.value?.models || []).filter((item) => !needle || item.model.toLowerCase().includes(needle)).map((item) => ({
    ...item,
    input: rateText(item, 'input'),
    output: rateText(item, 'output'),
    cache_read: rateText(item, 'cache_read'),
    cache_creation: rateText(item, 'cache_creation'),
    price_source: priceSourceText(item),
    threshold: item.long_context.threshold_tokens ? number(item.long_context.threshold_tokens) : '-',
    multipliers: item.long_context.threshold_tokens
      ? `${(item.long_context.input_multiplier_ppm / 1_000_000).toFixed(2)}x / ${(item.long_context.output_multiplier_ppm / 1_000_000).toFixed(2)}x`
      : '-',
  }))
})

async function load(silent = false) {
  if (!silent) loading.value = true
  error.value = ''
  try {
    data.value = await api(`/api/pricing${toQuery({ cycle: cycle.value })}`)
    if (!cycle.value && data.value?.billing?.cycle?.name) cycle.value = data.value.billing.cycle.name
  } catch (exc) {
    error.value = exc.message
  } finally {
    if (!silent) loading.value = false
  }
}

const autoRefresh = useAutoRefresh((silent) => load(silent), { interval: 60_000 })

watch(cycle, (value, previous) => { if (previous && value !== previous) autoRefresh.refresh() })
</script>

<template>
  <div class="content-shell">
    <PageHeader title="费用规则" :subtitle="data?.selected_version ? `${data.selected_version.name} · ${data.selected_version.source}` : '当前价格版本'">
      <template #actions>
        <v-select v-if="data?.billing?.cycles?.length" v-model="cycle" :items="data.billing.cycles" item-title="name" item-value="name" label="账期" style="width: 210px" />
        <v-tooltip text="刷新费用规则"><template #activator="{ props }"><v-btn v-bind="props" icon variant="outlined" :loading="loading" @click="autoRefresh.refresh()"><RefreshCw :size="18" /></v-btn></template></v-tooltip>
      </template>
    </PageHeader>

    <LoadingState :loading="loading" :error="error" :empty="!data" @retry="autoRefresh.refresh()">
      <v-alert v-if="data?.selected_version?.unpriced_events" type="warning" variant="tonal" border="start" class="mb-4">
        所选账期有 {{ number(data.selected_version.unpriced_events) }} 条未计价事件。
      </v-alert>
      <v-alert v-if="data?.billing?.cycle?.waiver" type="warning" variant="tonal" border="start" class="mb-4">
        <strong>{{ data.billing.cycle.name }} 数据质量说明：</strong>{{ data.billing.cycle.waiver }}
      </v-alert>

      <div class="two-column">
        <section class="section-band">
          <div class="section-band__head"><div><h2>梯度计费</h2><p>{{ data?.billing?.cycle?.gradient_rule || '默认规则' }} · 先按 Telegram 用户聚合全部 Key</p></div></div>
          <div class="section-band__body section-band__body--flush">
            <v-table density="compact">
              <thead><tr><th>等效成本区间 USD</th><th class="text-right">计费倍率</th></tr></thead>
              <tbody>
                <tr v-for="item in data?.billing?.tiers || []" :key="`${item.left_usd}-${item.right_usd}`">
                  <td class="mono">{{ item.left_usd }} - {{ item.right_usd ?? '∞' }}</td>
                  <td class="text-right mono">{{ item.multiplier }}x</td>
                </tr>
              </tbody>
            </v-table>
          </div>
        </section>
        <section class="section-band">
          <div class="section-band__head"><div><h2>资源池固定成本</h2><p>{{ data?.billing?.cycle?.name || '未选择账期' }} · 各池独立扣除按量 Key 后分摊</p></div></div>
          <div class="section-band__body section-band__body--flush">
            <v-table density="compact">
              <thead><tr><th>资源池</th><th>账号范围</th><th>模型范围</th><th class="text-right">固定成本</th></tr></thead>
              <tbody>
                <tr v-for="pool in data?.billing?.pools || []" :key="pool.id">
                  <td>{{ pool.name }}</td>
                  <td>{{ pool.rules.map(item => item.account_scope === 'all' ? '全部' : '受限').join('、') || '-' }}</td>
                  <td class="mono">{{ pool.rules.map(item => item.model_pattern || '全部').join('、') || '-' }}</td>
                  <td class="text-right mono">{{ money(pool.fixed_cost, '¥') }}</td>
                </tr>
              </tbody>
            </v-table>
          </div>
        </section>
      </div>

      <section class="section-band mt-4">
        <div class="section-band__head">
          <div><h2>模型价格</h2><p>单位：USD / 1M tokens · 版本 {{ data?.selected_version?.name }} · 激活 {{ dateTime(data?.selected_version?.activated_at) }}</p></div>
          <div class="d-flex ga-2 align-center flex-wrap">
            <v-btn-toggle v-model="tier" mandatory divided color="primary" density="compact">
              <v-btn v-for="item in tiers" :key="item.value" :value="item.value" size="small">{{ item.title }}</v-btn>
            </v-btn-toggle>
            <v-text-field v-model="search" label="搜索模型" clearable style="width: 220px">
              <template #prepend-inner><Search :size="16" /></template>
            </v-text-field>
          </div>
        </div>
        <div class="section-band__body section-band__body--flush">
          <v-data-table :headers="modelHeaders" :items="models" :items-per-page="50">
            <template #item.model="{ item }"><span class="mono">{{ item.model }}</span></template>
            <template #item.input="{ item }"><span class="mono">{{ item.input }}</span></template>
            <template #item.cache_read="{ item }"><span class="mono">{{ item.cache_read }}</span></template>
            <template #item.cache_creation="{ item }"><span class="mono">{{ item.cache_creation }}</span></template>
            <template #item.output="{ item }"><span class="mono">{{ item.output }}</span></template>
            <template #item.threshold="{ item }"><span class="mono">{{ item.threshold }}</span></template>
            <template #item.multipliers="{ item }"><span class="mono">{{ item.multipliers }}</span></template>
          </v-data-table>
        </div>
      </section>

      <section class="section-band">
        <div class="section-band__head"><div><h2>计价语义</h2><p>当前账务引擎固定规则</p></div></div>
        <div class="section-band__body semantics-grid">
          <div><span>Cached tokens</span><strong>Input 子集</strong></div>
          <div><span>Reasoning tokens</span><strong>Output 子集</strong></div>
          <div><span>长上下文判定</span><strong>总 Input</strong></div>
          <div><span>未归属普通 Key</span><strong>不计费</strong></div>
          <div><span>未归属按量 Key</span><strong>USD 成本 × 倍率</strong></div>
          <div><span>分摊取整</span><strong>最大余数法</strong></div>
        </div>
      </section>
    </LoadingState>
  </div>
</template>

<style scoped>
.semantics-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1px; background: #dfe5e2; padding: 1px; }
.semantics-grid > div { background: #fff; padding: 13px; }
.semantics-grid span { display: block; color: #68716e; font-size: .72rem; }
.semantics-grid strong { display: block; margin-top: 5px; }
@media (max-width: 900px) { .semantics-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
