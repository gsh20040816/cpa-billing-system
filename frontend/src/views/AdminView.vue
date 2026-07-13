<script setup>
import { computed, reactive, ref } from 'vue'
import {
  Activity, ArrowDown, ArrowUp, CloudDownload, Edit3, KeyRound, Plus, RefreshCw, ShieldCheck,
  Save, Scale, Search, Shuffle, Trash2, XCircle,
} from '@lucide/vue'
import { api } from '../api'
import LoadingState from '../components/LoadingState.vue'
import MetricRail from '../components/MetricRail.vue'
import PageHeader from '../components/PageHeader.vue'
import { useAutoRefresh } from '../lib/autoRefresh'
import { dateTime, money, number } from '../lib/format'

const loading = ref(true)
const mutating = ref(false)
const error = ref('')
const data = ref(null)
const tab = ref('overview')
const snackbar = reactive({ show: false, text: '', color: 'success' })

const cycleDialog = reactive({
  open: false, name: '', start: '', end: '', gradient_rule_id: null, pool_costs: {}, waiver: '',
})
const cycleConfigDialog = reactive({
  open: false, cycle: null, gradient_rule_id: null, pool_costs: {}, reason: '',
})
const closeDialog = reactive({ open: false, cycle: null, confirm_close: false, confirm_waiver: false })
const adjustmentDialog = reactive({ open: false, cycle: '', telegram_user_id: '', amount_cents: '', reason: '' })
const manualUsageDialog = reactive({
  open: false, id: null, cycle: '', pool_id: null, telegram_user_id: null, amount_usd: '', reason: '',
})
const transferDialog = reactive({ open: false, key_id: '', telegram_user_id: '', reason: '', confirm_transfer: false })
const poolDialog = reactive({ open: false, name: '', auth_pattern: '', model_pattern: '', priority: 100 })
const pricingDialog = reactive({ open: false, name: '', reason: '' })
const gradientDialog = reactive({ open: false, id: null, name: '', description: '', reason: '', tiers: [] })
const deleteGradientDialog = reactive({ open: false, rule: null, reason: '' })
const keyProfileDialog = reactive({ open: false, key: null, name: '', multiplier: '', reason: '' })
const userAdminDialog = reactive({ open: false, user: null, is_admin: false, reason: '' })
const resetQuotaDialog = reactive({ open: false, account: null, reason: '' })
const pricingSearch = ref('')
const pricingRuleDialog = reactive({
  open: false,
  editing_model: '',
  model: '',
  input_usd_per_million: '',
  output_usd_per_million: '',
  cache_read_usd_per_million: '',
  cache_creation_usd_per_million: '',
  priority_input_usd_per_million: '',
  priority_output_usd_per_million: '',
  priority_cache_read_usd_per_million: '',
  priority_cache_creation_usd_per_million: '',
  flex_input_usd_per_million: '',
  flex_output_usd_per_million: '',
  long_context_threshold_tokens: '',
  long_context_input_multiplier: '1',
  long_context_output_multiplier: '1',
  version_name: '',
  reason: '',
})

const reconciliation = computed(() => data.value?.reconciliation || {})
const admin = computed(() => data.value?.admin || {})
const activeGradients = computed(() => (admin.value.gradients || []).filter((item) => item.active))
const activePools = computed(() => (admin.value.pools || []).filter((item) => item.active))
const openCycles = computed(() => (admin.value.cycles || []).filter((item) => item.status !== 'closed'))
const registeredUsers = computed(() => (admin.value.users || []).filter((item) => item.registered))
const pricingModels = computed(() => {
  const needle = pricingSearch.value.trim().toLowerCase()
  return (admin.value.pricing_rules?.models || []).filter((item) => !needle || item.model.toLowerCase().includes(needle))
})
const manualUsagePools = computed(() => {
  const cycle = openCycles.value.find((item) => item.name === manualUsageDialog.cycle)
  return cycle?.pool_costs || []
})
const metrics = computed(() => [
  { label: 'CPAMP 事件', value: number(reconciliation.value.cpamp_events), mono: true },
  { label: '镜像事件', value: number(reconciliation.value.raw_events), mono: true },
  { label: '已计价', value: number(reconciliation.value.rated_events), mono: true },
  { label: '同步待处理', value: number(reconciliation.value.sync_backlog), mono: true },
  { label: '未计价', value: number(reconciliation.value.unpriced_events), mono: true },
  { label: '24h 等效成本', value: money(data.value?.usage?.recent_cost), mono: true },
])

function notify(text, color = 'success') {
  snackbar.text = text
  snackbar.color = color
  snackbar.show = true
}

async function load(silent = false) {
  if (!silent) loading.value = true
  error.value = ''
  try {
    data.value = await api('/api/admin/snapshot', { admin: true })
  } catch (exc) {
    error.value = exc.message
  } finally {
    if (!silent) loading.value = false
  }
}

async function mutate(path, body, success, method = 'POST') {
  mutating.value = true
  try {
    const result = await api(path, { admin: true, body, method })
    notify(success)
    await autoRefresh.refresh()
    return result || true
  } catch (exc) {
    notify(exc.message, 'error')
    return null
  } finally {
    mutating.value = false
  }
}

function openResetQuota(account) {
  Object.assign(resetQuotaDialog, { open: true, account, reason: '' })
}

async function resetQuota() {
  const account = resetQuotaDialog.account
  if (!account || !resetQuotaDialog.reason.trim()) {
    notify('请填写重置原因', 'warning')
    return
  }
  const result = await mutate(
    `/api/admin/accounts/${encodeURIComponent(account.id)}/reset-quota`,
    { reason: resetQuotaDialog.reason.trim() },
    '已清除 CPA 本地额度状态；这不是上游套餐额度充值',
  )
  if (result) resetQuotaDialog.open = false
}

function initializePoolCosts(target, existing = []) {
  target.pool_costs = {}
  activePools.value.forEach((pool) => {
    const current = existing.find((item) => item.pool_id === pool.id)
    target.pool_costs[pool.id] = current?.fixed_cost || '0.00'
  })
}

function poolCostsPayload(target) {
  return activePools.value.map((pool) => ({
    pool_id: pool.id,
    fixed_cost: String(target.pool_costs[pool.id] || '0'),
  }))
}

function openCreateCycle() {
  Object.assign(cycleDialog, {
    open: true,
    name: '',
    start: '',
    end: '',
    gradient_rule_id: activeGradients.value[0]?.id || null,
    waiver: '',
  })
  initializePoolCosts(cycleDialog)
}

async function createCycle() {
  const result = await mutate('/api/admin/cycles', {
    name: cycleDialog.name,
    start: cycleDialog.start,
    end: cycleDialog.end,
    fixed_cost: '0',
    gradient_rule_id: cycleDialog.gradient_rule_id,
    pool_costs: poolCostsPayload(cycleDialog),
    waiver: cycleDialog.waiver || null,
  }, '账期已创建')
  if (result) cycleDialog.open = false
}

function openCycleConfig(cycle) {
  Object.assign(cycleConfigDialog, {
    open: true,
    cycle,
    gradient_rule_id: cycle.gradient_rule_id,
    reason: '',
  })
  initializePoolCosts(cycleConfigDialog, cycle.pool_costs || [])
}

async function saveCycleConfig() {
  const cycle = cycleConfigDialog.cycle
  const result = await mutate(`/api/admin/cycles/${encodeURIComponent(cycle.name)}/configuration`, {
    gradient_rule_id: cycleConfigDialog.gradient_rule_id,
    pool_costs: poolCostsPayload(cycleConfigDialog),
    reason: cycleConfigDialog.reason,
  }, '账期计费配置已更新', 'PUT')
  if (result) cycleConfigDialog.open = false
}

async function previewCycle(name) {
  await mutate(`/api/admin/cycles/${encodeURIComponent(name)}/preview`, {}, '账单预览快照已更新')
}

function openClose(cycle) {
  Object.assign(closeDialog, { open: true, cycle, confirm_close: false, confirm_waiver: false })
}

async function closeCycle() {
  const result = await mutate(`/api/admin/cycles/${encodeURIComponent(closeDialog.cycle.name)}/close`, {
    confirm_close: closeDialog.confirm_close,
    confirm_waiver: closeDialog.confirm_waiver,
  }, '账期已关闭并冻结')
  if (result) closeDialog.open = false
}

async function createAdjustment() {
  const result = await mutate('/api/admin/adjustments', {
    cycle: adjustmentDialog.cycle,
    telegram_user_id: Number(adjustmentDialog.telegram_user_id),
    amount_cents: Number(adjustmentDialog.amount_cents),
    reason: adjustmentDialog.reason,
  }, '人工调整已添加')
  if (result) adjustmentDialog.open = false
}

function selectManualUsageCycle(cycleName) {
  manualUsageDialog.cycle = cycleName
  const cycle = openCycles.value.find((item) => item.name === cycleName)
  manualUsageDialog.pool_id = cycle?.pool_costs?.[0]?.pool_id || null
}

function openManualUsage(item = null) {
  if (item) {
    Object.assign(manualUsageDialog, {
      open: true,
      id: item.id,
      cycle: item.cycle,
      pool_id: item.pool_id,
      telegram_user_id: item.user_id,
      amount_usd: item.amount_usd,
      reason: item.reason,
    })
    return
  }
  const cycle = openCycles.value[0]
  Object.assign(manualUsageDialog, {
    open: true,
    id: null,
    cycle: cycle?.name || '',
    pool_id: cycle?.pool_costs?.[0]?.pool_id || null,
    telegram_user_id: registeredUsers.value[0]?.id || null,
    amount_usd: '',
    reason: '',
  })
}

async function saveManualUsage() {
  const editing = Boolean(manualUsageDialog.id)
  const path = editing
    ? `/api/admin/manual-usage-adjustments/${manualUsageDialog.id}`
    : '/api/admin/manual-usage-adjustments'
  const result = await mutate(path, {
    cycle: manualUsageDialog.cycle,
    pool_id: Number(manualUsageDialog.pool_id),
    telegram_user_id: Number(manualUsageDialog.telegram_user_id),
    amount_usd: String(manualUsageDialog.amount_usd),
    reason: manualUsageDialog.reason,
  }, editing ? '补录信息已更新并重新计算账期' : '原始等效用量已计入账期', editing ? 'PUT' : 'POST')
  if (result) manualUsageDialog.open = false
}

async function transferOwnership() {
  const result = await mutate('/api/admin/ownership-transfers', {
    key_id: Number(transferDialog.key_id),
    telegram_user_id: Number(transferDialog.telegram_user_id),
    reason: transferDialog.reason,
    confirm_transfer: transferDialog.confirm_transfer,
  }, 'Key 归属已变更')
  if (result) transferDialog.open = false
}

async function createPool() {
  const result = await mutate('/api/admin/pools', {
    name: poolDialog.name,
    auth_pattern: poolDialog.auth_pattern || null,
    model_pattern: poolDialog.model_pattern || null,
    priority: Number(poolDialog.priority),
  }, '资源池已创建')
  if (result) poolDialog.open = false
}

async function syncPricing() {
  const result = await mutate('/api/admin/pricing-versions/sync', {
    name: pricingDialog.name || null,
    reason: pricingDialog.reason,
  }, '上游价格已同步，未关闭账期已提交后台重算')
  if (result) pricingDialog.open = false
}

function pricingValue(rule, tier, field) {
  return rule?.[tier]?.[field]?.usd_per_million || '-'
}

function pricingLine(rule, tier) {
  return `I ${pricingValue(rule, tier, 'input')} · R ${pricingValue(rule, tier, 'cache_read')} · C+ ${pricingValue(rule, tier, 'cache_creation')} · O ${pricingValue(rule, tier, 'output')}`
}

function pricingContext(rule) {
  const context = rule?.long_context || {}
  if (!context.threshold_tokens) return '-'
  return `${number(context.threshold_tokens)} · ${(Number(context.input_multiplier_ppm || 1_000_000) / 1_000_000).toFixed(2)}x / ${(Number(context.output_multiplier_ppm || 1_000_000) / 1_000_000).toFixed(2)}x`
}

function ruleField(rule, tier, field) {
  return rule?.[tier]?.[field]?.usd_per_million || ''
}

function multiplierField(rule, field) {
  const value = rule?.long_context?.[field]
  return value === undefined || value === null ? '1' : String(Number(value) / 1_000_000)
}

function openPricingRule(rule = null) {
  Object.assign(pricingRuleDialog, {
    open: true,
    editing_model: rule?.model || '',
    model: rule?.model || '',
    input_usd_per_million: ruleField(rule, 'default', 'input'),
    output_usd_per_million: ruleField(rule, 'default', 'output'),
    cache_read_usd_per_million: ruleField(rule, 'default', 'cache_read'),
    cache_creation_usd_per_million: ruleField(rule, 'default', 'cache_creation'),
    priority_input_usd_per_million: ruleField(rule, 'priority', 'input'),
    priority_output_usd_per_million: ruleField(rule, 'priority', 'output'),
    priority_cache_read_usd_per_million: ruleField(rule, 'priority', 'cache_read'),
    priority_cache_creation_usd_per_million: ruleField(rule, 'priority', 'cache_creation'),
    flex_input_usd_per_million: ruleField(rule, 'flex', 'input'),
    flex_output_usd_per_million: ruleField(rule, 'flex', 'output'),
    long_context_threshold_tokens: rule?.long_context?.threshold_tokens ? String(rule.long_context.threshold_tokens) : '',
    long_context_input_multiplier: multiplierField(rule, 'input_multiplier_ppm'),
    long_context_output_multiplier: multiplierField(rule, 'output_multiplier_ppm'),
    version_name: '',
    reason: '',
  })
}

async function savePricingRule() {
  const optional = (value) => String(value || '').trim() || null
  const result = await mutate('/api/admin/pricing-rules', {
    model: pricingRuleDialog.model.trim(),
    input_usd_per_million: pricingRuleDialog.input_usd_per_million,
    output_usd_per_million: pricingRuleDialog.output_usd_per_million,
    cache_read_usd_per_million: pricingRuleDialog.cache_read_usd_per_million,
    cache_creation_usd_per_million: pricingRuleDialog.cache_creation_usd_per_million,
    priority_input_usd_per_million: optional(pricingRuleDialog.priority_input_usd_per_million),
    priority_output_usd_per_million: optional(pricingRuleDialog.priority_output_usd_per_million),
    priority_cache_read_usd_per_million: optional(pricingRuleDialog.priority_cache_read_usd_per_million),
    priority_cache_creation_usd_per_million: optional(pricingRuleDialog.priority_cache_creation_usd_per_million),
    flex_input_usd_per_million: optional(pricingRuleDialog.flex_input_usd_per_million),
    flex_output_usd_per_million: optional(pricingRuleDialog.flex_output_usd_per_million),
    long_context_threshold_tokens: optional(pricingRuleDialog.long_context_threshold_tokens) === null
      ? null : Number(pricingRuleDialog.long_context_threshold_tokens),
    long_context_input_multiplier: pricingRuleDialog.long_context_input_multiplier,
    long_context_output_multiplier: pricingRuleDialog.long_context_output_multiplier,
    version_name: optional(pricingRuleDialog.version_name),
    reason: pricingRuleDialog.reason,
  }, '手动计费规则已保存，未关闭账期已提交后台重算', 'PUT')
  if (result) pricingRuleDialog.open = false
}

async function syncCpaKeys() {
  await mutate('/api/admin/cpa-keys/sync', {}, 'CPA 当前 API Key 已同步')
}

function openUserAdmin(user) {
  Object.assign(userAdminDialog, {
    open: true,
    user,
    is_admin: !user.is_admin,
    reason: '',
  })
}

async function saveUserAdmin() {
  const result = await mutate(
    `/api/admin/users/${userAdminDialog.user.id}/admin`,
    { is_admin: userAdminDialog.is_admin, reason: userAdminDialog.reason },
    userAdminDialog.is_admin ? '已授予 Web 管理权限' : '已撤销 Web 管理权限',
    'PATCH',
  )
  if (result) userAdminDialog.open = false
}

function emptyTier() {
  return { left: '0', right: '', multiplier: '1' }
}

function openGradient(rule = null) {
  gradientDialog.open = true
  gradientDialog.id = rule?.id || null
  gradientDialog.name = rule?.name || ''
  gradientDialog.description = rule?.description || ''
  gradientDialog.reason = ''
  gradientDialog.tiers = rule
    ? rule.tiers.map((item) => ({
      left: String(item.left), right: item.right === null ? '' : String(item.right), multiplier: String(item.multiplier),
    }))
    : [emptyTier()]
}

function addTier() {
  const previous = gradientDialog.tiers.at(-1)
  const left = String(Number(previous?.left || 0) + 100)
  if (previous) previous.right = left
  gradientDialog.tiers.push({ left, right: '', multiplier: previous?.multiplier || '1' })
}

function normalizeTiers() {
  gradientDialog.tiers.forEach((tier, index) => {
    tier.left = index === 0 ? '0' : gradientDialog.tiers[index - 1].right
    if (index < gradientDialog.tiers.length - 1 && (!tier.right || Number(tier.right) <= Number(tier.left))) {
      tier.right = String(Number(tier.left || 0) + 100)
    }
  })
  gradientDialog.tiers.at(-1).right = ''
}

function removeTier(index) {
  if (gradientDialog.tiers.length === 1) return
  gradientDialog.tiers.splice(index, 1)
  normalizeTiers()
}

function moveTier(index, direction) {
  const target = index + direction
  if (target < 0 || target >= gradientDialog.tiers.length) return
  const currentMultiplier = gradientDialog.tiers[index].multiplier
  gradientDialog.tiers[index].multiplier = gradientDialog.tiers[target].multiplier
  gradientDialog.tiers[target].multiplier = currentMultiplier
}

async function saveGradient() {
  const body = {
    name: gradientDialog.name,
    description: gradientDialog.description || null,
    tiers: gradientDialog.tiers.map((item, index) => ({
      left: item.left,
      right: index === gradientDialog.tiers.length - 1 ? null : item.right,
      multiplier: item.multiplier,
    })),
    reason: gradientDialog.reason,
  }
  const result = gradientDialog.id
    ? await mutate(`/api/admin/gradient-rules/${gradientDialog.id}`, body, '梯度规则已更新，关联开放账期已重算', 'PUT')
    : await mutate('/api/admin/gradient-rules', body, '梯度规则已创建')
  if (result) gradientDialog.open = false
}

function openDeleteGradient(rule) {
  Object.assign(deleteGradientDialog, { open: true, rule, reason: '' })
}

async function deleteGradient() {
  const result = await mutate(
    `/api/admin/gradient-rules/${deleteGradientDialog.rule.id}`,
    { reason: deleteGradientDialog.reason },
    '梯度规则已停用',
    'DELETE',
  )
  if (result) deleteGradientDialog.open = false
}

function openKeyProfile(key) {
  Object.assign(keyProfileDialog, {
    open: true,
    key,
    name: key.name || '',
    multiplier: key.billing_multiplier || '',
    reason: '',
  })
}

async function saveKeyProfile() {
  const result = await mutate(`/api/admin/keys/${keyProfileDialog.key.id}/billing-profile`, {
    name: keyProfileDialog.name || null,
    multiplier: keyProfileDialog.multiplier || null,
    reason: keyProfileDialog.reason || null,
  }, '未绑定 Key 计费档案已更新', 'PATCH')
  if (result) keyProfileDialog.open = false
}

const autoRefresh = useAutoRefresh((silent) => load(silent), { interval: 30_000 })
</script>

<template>
  <div class="content-shell content-shell--admin">
    <PageHeader eyebrow="Web administration" title="系统管理" subtitle="计价、规则、归属与结算使用统一 Web 管理权限">
      <template #actions>
        <v-tooltip text="刷新管理数据">
          <template #activator="{ props }">
            <v-btn v-bind="props" icon variant="outlined" :loading="loading" @click="autoRefresh.refresh()"><RefreshCw :size="18" /></v-btn>
          </template>
        </v-tooltip>
      </template>
    </PageHeader>

    <LoadingState :loading="loading" :error="error" :empty="!data" @retry="autoRefresh.refresh()">
      <v-alert v-if="!reconciliation.ok" type="error" variant="tonal" border="start" class="mb-4">
        对账完整性异常：Dead letter {{ number(reconciliation.dead_letters) }}，镜像多出 {{ number(reconciliation.raw_excess) }}，未分池 {{ number(reconciliation.unassigned_events) }}。
      </v-alert>
      <v-alert v-else-if="reconciliation.sync_degraded || reconciliation.rating_pending" type="warning" variant="tonal" border="start" class="mb-4">
        数据处理需要关注：同步待处理 {{ number(reconciliation.sync_backlog) }}，未计价 {{ number(reconciliation.unpriced_events) }}，最后同步 {{ dateTime(reconciliation.last_sync_at) }}。
      </v-alert>
      <MetricRail :items="metrics" :columns="6" />

      <v-tabs v-model="tab" color="primary" class="admin-tabs" show-arrows>
        <v-tab value="overview">运行概览</v-tab>
        <v-tab value="cycles">账期</v-tab>
        <v-tab value="identity">用户与 Keys</v-tab>
        <v-tab value="rules">计费规则</v-tab>
        <v-tab value="adjustments">调整与归属</v-tab>
        <v-tab value="audit">审计</v-tab>
      </v-tabs>

      <v-window v-model="tab" class="mt-4">
        <v-window-item value="overview">
          <section class="section-band">
            <div class="section-band__head"><div><h2>同步状态</h2><p>CPAMP 增量游标和待处理事件</p></div></div>
            <div class="section-band__body section-band__body--flush">
              <v-table density="compact">
                <thead><tr><th>来源</th><th>Last ID</th><th>最后事件</th><th>最后成功</th><th class="text-right">Backlog</th><th>错误</th></tr></thead>
                <tbody><tr v-for="item in admin.sync || []" :key="item.source"><td>{{ item.source }}</td><td class="mono">{{ number(item.last_event_id) }}</td><td>{{ item.last_event_at }}</td><td>{{ item.last_success_at }}</td><td class="text-right mono">{{ number(item.backlog) }}</td><td :class="item.last_error ? 'data-error' : 'data-muted'">{{ item.last_error || '-' }}</td></tr></tbody>
              </v-table>
            </div>
          </section>
          <section class="section-band">
            <div class="section-band__head"><div><h2>上游账号</h2><p>额度与窗口来自 CPA 上游读取，用量与费用来自本地账本</p></div></div>
            <div class="section-band__body section-band__body--flush">
              <v-table density="compact">
                <thead><tr><th>账号</th><th>类型</th><th>计划</th><th class="text-right">请求</th><th class="text-right">Tokens</th><th class="text-right">费用</th><th>额度</th><th class="text-right">操作</th></tr></thead>
                <tbody><tr v-for="item in data?.accounts?.accounts || []" :key="item.id"><td>{{ item.name }}</td><td>{{ item.type }}</td><td>{{ item.plan_type }}</td><td class="text-right mono">{{ number(item.usage.requests) }}</td><td class="text-right mono">{{ number(item.usage.total_tokens) }}</td><td class="text-right mono">{{ money(item.usage.cost) }}</td><td>{{ item.quota.map(q => `${q.label} ${q.used_percent}%`).join(' · ') || '-' }}</td><td class="text-right"><v-btn v-if="item.can_refresh" size="small" variant="text" color="warning" @click="openResetQuota(item)"><RefreshCw :size="15" class="mr-1" />重置本地状态</v-btn><span v-else class="data-muted">不可用</span></td></tr></tbody>
              </v-table>
            </div>
          </section>
        </v-window-item>

        <v-window-item value="cycles">
          <section class="section-band">
            <div class="section-band__head">
              <div><h2>账期</h2><p>开放账期实时估算，关闭后冻结价格、规则和金额</p></div>
              <v-btn color="primary" size="small" @click="openCreateCycle"><Plus :size="16" class="mr-2" />创建账期</v-btn>
            </div>
            <div class="section-band__body section-band__body--flush">
              <v-table density="compact">
                <thead><tr><th>名称</th><th>时间</th><th>价格版本</th><th>梯度规则</th><th>资源池成本</th><th>状态</th><th class="text-right">操作</th></tr></thead>
                <tbody>
                  <tr v-for="item in admin.cycles || []" :key="item.name">
                    <td><div class="mono">{{ item.name }}</div><div v-if="item.waiver" class="data-muted text-caption admin-wrap">{{ item.waiver }}</div></td>
                    <td><div>{{ item.start }}</div><div class="data-muted text-caption">至 {{ item.end }}</div></td>
                    <td>{{ item.pricing_version || '-' }}</td>
                    <td>{{ item.gradient_rule || '-' }}</td>
                    <td><div v-for="cost in item.pool_costs" :key="cost.pool_id">{{ cost.pool }} <span class="mono">{{ money(cost.fixed_cost, '¥') }}</span></div></td>
                    <td><v-chip :color="item.status === 'closed' ? 'default' : 'primary'" variant="tonal">{{ item.status }}</v-chip></td>
                    <td class="text-right admin-actions">
                      <template v-if="item.status !== 'closed'">
                        <v-btn size="small" variant="text" @click="openCycleConfig(item)"><Edit3 :size="15" class="mr-1" />配置</v-btn>
                        <v-btn size="small" variant="text" @click="previewCycle(item.name)"><Scale :size="15" class="mr-1" />快照</v-btn>
                        <v-btn size="small" variant="text" color="error" @click="openClose(item)"><XCircle :size="15" class="mr-1" />关闭</v-btn>
                      </template>
                    </td>
                  </tr>
                </tbody>
              </v-table>
            </div>
          </section>
        </v-window-item>

        <v-window-item value="identity">
          <section class="section-band">
            <div class="section-band__head"><div><h2>Telegram 用户</h2><p>注册、授权和 Web 管理权限；配置文件中的管理员不能在此撤销</p></div></div>
            <div class="section-band__body section-band__body--flush">
              <v-data-table
                :headers="[
                  {title:'ID',key:'id'},{title:'用户',key:'name'},{title:'已注册',key:'registered'},
                  {title:'手动授权',key:'manual_allowed'},{title:'有效 Keys',key:'active_keys'},
                  {title:'Web 管理员',key:'is_admin'},{title:'操作',key:'actions',sortable:false},
                ]"
                :items="admin.users || []"
                :items-per-page="25"
              >
                <template #item.is_admin="{ item }"><v-chip :color="item.is_admin ? 'secondary' : 'default'" variant="tonal">{{ item.is_admin ? '是' : '否' }}</v-chip></template>
                <template #item.actions="{ item }"><v-btn v-if="!item.configured_admin" size="small" variant="text" @click="openUserAdmin(item)"><ShieldCheck :size="15" class="mr-1" />{{ item.is_admin ? '撤销管理' : '授予管理' }}</v-btn><span v-else class="data-muted">配置管理员</span></template>
              </v-data-table>
            </div>
          </section>
          <section class="section-band">
            <div class="section-band__head">
              <div><h2>全部 API Keys</h2><p>包含 CPA 当前 Key，只显示掩码；未绑定 Key 可设置别名和人民币/美元倍率</p></div>
              <v-btn size="small" variant="outlined" :loading="mutating" @click="syncCpaKeys"><RefreshCw :size="16" class="mr-2" />同步 CPA Keys</v-btn>
            </div>
            <div class="section-band__body section-band__body--flush">
              <v-data-table
                :headers="[
                  {title:'ID',key:'id'},{title:'Key',key:'masked'},{title:'别名',key:'name'},
                  {title:'CPA 当前存在',key:'present_in_cpa'},{title:'状态',key:'status'},
                  {title:'归属',key:'owner'},{title:'倍率',key:'billing_multiplier'},
                  {title:'最后确认',key:'last_seen_in_cpa_at'},{title:'操作',key:'actions',sortable:false},
                ]"
                :items="admin.keys || []"
                :items-per-page="50"
              >
                <template #item.masked="{ item }"><span class="mono">{{ item.masked }}</span></template>
                <template #item.present_in_cpa="{ item }"><v-chip :color="item.present_in_cpa ? 'success' : 'default'" variant="tonal">{{ item.present_in_cpa ? '是' : '否' }}</v-chip></template>
                <template #item.billing_multiplier="{ item }"><span class="mono">{{ item.billing_multiplier ? `${item.billing_multiplier}x` : '-' }}</span></template>
                <template #item.last_seen_in_cpa_at="{ item }">{{ dateTime(item.last_seen_in_cpa_at) }}</template>
                <template #item.actions="{ item }"><v-btn v-if="item.billing_profile_editable" size="small" variant="text" @click="openKeyProfile(item)"><KeyRound :size="15" class="mr-1" />计费档案</v-btn></template>
              </v-data-table>
            </div>
          </section>
          <section class="section-band">
            <div class="section-band__head"><div><h2>归属历史</h2><p>最近 100 条，关闭账期不会被未来归属变化改写</p></div></div>
            <div class="section-band__body section-band__body--flush">
              <v-data-table :headers="[{title:'Key',key:'key'},{title:'用户',key:'user'},{title:'开始',key:'from'},{title:'结束',key:'to'},{title:'来源',key:'source'},{title:'原因',key:'reason'}]" :items="admin.ownership || []" :items-per-page="25"><template #item.key="{ item }"><span class="mono">#{{ item.key_id }} {{ item.key }}</span></template></v-data-table>
            </div>
          </section>
        </v-window-item>

        <v-window-item value="rules">
          <section class="section-band">
            <div class="section-band__head">
              <div><h2>梯度规则</h2><p>修改会传播到关联的未关闭账期；关闭账期保留快照</p></div>
              <v-btn size="small" color="primary" @click="openGradient()"><Plus :size="16" class="mr-2" />新增规则</v-btn>
            </div>
            <div class="section-band__body section-band__body--flush">
              <v-table density="compact">
                <thead><tr><th>规则</th><th>区间</th><th>开放账期</th><th>状态</th><th>更新时间</th><th class="text-right">操作</th></tr></thead>
                <tbody>
                  <tr v-for="rule in admin.gradients || []" :key="rule.id">
                    <td><strong>{{ rule.name }}</strong><div class="data-muted text-caption">{{ rule.description || '-' }}</div></td>
                    <td class="tier-summary"><span v-for="tier in rule.tiers" :key="`${tier.left}-${tier.right}`" class="mono">{{ tier.left }}-{{ tier.right ?? '∞' }}: {{ tier.multiplier }}x</span></td>
                    <td class="mono">{{ number(rule.open_cycle_count) }}</td>
                    <td><v-chip :color="rule.active ? 'success' : 'default'" variant="tonal">{{ rule.active ? 'active' : 'inactive' }}</v-chip></td>
                    <td>{{ dateTime(rule.updated_at) }}</td>
                    <td class="text-right admin-actions"><template v-if="rule.active"><v-btn size="small" variant="text" @click="openGradient(rule)"><Edit3 :size="15" class="mr-1" />编辑</v-btn><v-btn size="small" variant="text" color="error" @click="openDeleteGradient(rule)"><Trash2 :size="15" class="mr-1" />停用</v-btn></template></td>
                  </tr>
                </tbody>
              </v-table>
            </div>
          </section>

          <div class="two-column mt-4">
            <section class="section-band">
              <div class="section-band__head"><div><h2>资源池</h2><p>按账号或模型将请求归入独立成本池</p></div><v-btn size="small" variant="outlined" @click="poolDialog.open = true"><Plus :size="16" class="mr-2" />创建资源池</v-btn></div>
              <div class="section-band__body section-band__body--flush">
                <v-table density="compact"><thead><tr><th>名称</th><th>优先级</th><th>账号规则</th><th>模型规则</th></tr></thead><tbody><tr v-for="pool in admin.pools || []" :key="pool.id"><td>{{ pool.name }}</td><td class="mono">{{ pool.rules.map(item => item.priority).join(', ') || '-' }}</td><td class="mono admin-wrap">{{ pool.rules.map(item => item.auth_index_pattern || '全部').join(', ') || '-' }}</td><td class="mono admin-wrap">{{ pool.rules.map(item => item.model_pattern || '全部').join(', ') || '-' }}</td></tr></tbody></v-table>
              </div>
            </section>
            <section class="section-band">
              <div class="section-band__head"><div><h2>模型价格版本</h2><p>从 CPAMP 的 LiteLLM/OpenRouter 上游同步</p></div><v-btn size="small" color="secondary" @click="pricingDialog.open = true"><CloudDownload :size="16" class="mr-2" />同步上游价格</v-btn></div>
              <div class="section-band__body section-band__body--flush">
                <v-table density="compact"><thead><tr><th>版本</th><th>状态</th><th>来源</th><th>激活时间</th></tr></thead><tbody><tr v-for="item in admin.pricing || []" :key="item.id"><td>{{ item.name }}</td><td><v-chip :color="item.status === 'active' ? 'success' : 'default'" variant="tonal">{{ item.status }}</v-chip></td><td>{{ item.source }}</td><td>{{ dateTime(item.activated_at) }}</td></tr></tbody></v-table>
              </div>
            </section>
          </div>

          <section class="section-band mt-4">
            <div class="section-band__head">
              <div>
                <h2>模型计费规则</h2>
                <p>
                  当前 active 版本：{{ admin.pricing_rules?.active_version?.name || '-' }} ·
                  手动保存会创建新版本并提交未关闭账期后台重算，关闭账期保持不变
                </p>
              </div>
              <div class="d-flex align-center ga-2 flex-wrap">
                <v-text-field v-model="pricingSearch" label="搜索模型" clearable style="width: 220px">
                  <template #prepend-inner><Search :size="16" /></template>
                </v-text-field>
                <v-btn size="small" color="primary" @click="openPricingRule()"><Plus :size="16" class="mr-2" />新增模型规则</v-btn>
              </div>
            </div>
            <div class="section-band__body section-band__body--flush">
              <v-alert type="info" variant="tonal" class="ma-4">
                上游同步仍可使用。同步会创建新的上游价格版本，并提交未关闭账期后台重算；历史版本和已关闭账期不会被修改。
              </v-alert>
              <v-data-table
                :headers="[
                  {title:'模型',key:'model',minWidth:190},
                  {title:'Default USD / 1M（I · R · C+ · O）',key:'default',minWidth:300},
                  {title:'Priority USD / 1M（I · R · C+ · O）',key:'priority',minWidth:300},
                  {title:'长上下文',key:'long_context',minWidth:170},
                  {title:'操作',key:'actions',sortable:false,width:110},
                ]"
                :items="pricingModels"
                :items-per-page="25"
              >
                <template #item.model="{ item }"><span class="mono">{{ item.model }}</span></template>
                <template #item.default="{ item }"><span class="mono">{{ pricingLine(item, 'default') }}</span></template>
                <template #item.priority="{ item }"><span class="mono">{{ pricingLine(item, 'priority') }}</span></template>
                <template #item.long_context="{ item }"><span class="mono">{{ pricingContext(item) }}</span></template>
                <template #item.actions="{ item }"><v-btn size="small" variant="text" @click="openPricingRule(item)"><Edit3 :size="15" class="mr-1" />调整</v-btn></template>
              </v-data-table>
            </div>
          </section>
        </v-window-item>

        <v-window-item value="adjustments">
          <div class="d-flex flex-wrap ga-2 mb-4"><v-btn color="primary" :disabled="!openCycles.length || !registeredUsers.length" @click="openManualUsage()"><Activity :size="16" class="mr-2" />添加原始用量</v-btn><v-btn variant="outlined" @click="adjustmentDialog.open = true"><Scale :size="16" class="mr-2" />调整最终费用</v-btn><v-btn color="secondary" @click="transferDialog.open = true"><Shuffle :size="16" class="mr-2" />变更归属</v-btn></div>
          <section class="section-band">
            <div class="section-band__head"><div><h2>手动原始用量记录</h2><p>全部记录均可分页查看；未关闭账期中的记录可以编辑更新</p></div></div>
            <div class="section-band__body section-band__body--flush">
              <v-data-table
                :headers="[{title:'账期',key:'cycle'},{title:'资源池',key:'pool'},{title:'用户',key:'user'},{title:'等效用量',key:'amount_usd'},{title:'原因',key:'reason'},{title:'最近变更',key:'at'},{title:'操作',key:'actions',sortable:false}]"
                :items="admin.manual_usage_adjustments || []"
                :items-per-page="25"
              >
                <template #item.user="{ item }"><div>{{ item.user }}</div><div class="data-muted text-caption mono">{{ item.user_id }}</div></template>
                <template #item.amount_usd="{ item }"><span class="mono">{{ money(item.amount_usd) }}</span></template>
                <template #item.at="{ item }"><div>{{ item.updated_at || item.created_at }}</div><div v-if="item.updated_at" class="data-muted text-caption">创建于 {{ item.created_at }}</div></template>
                <template #item.actions="{ item }"><v-btn v-if="item.editable" size="small" variant="text" @click="openManualUsage(item)"><Edit3 :size="15" class="mr-1" />编辑</v-btn><span v-else class="data-muted">已冻结</span></template>
              </v-data-table>
            </div>
          </section>
          <section class="section-band">
            <div class="section-band__head"><div><h2>最近最终费用调整</h2><p>在资源池分摊完成后直接增减人民币应付金额</p></div></div>
            <div class="section-band__body section-band__body--flush"><v-data-table :headers="[{title:'账期',key:'cycle'},{title:'用户',key:'user_id'},{title:'金额',key:'amount'},{title:'原因',key:'reason'},{title:'时间',key:'at'}]" :items="admin.adjustments || []" :items-per-page="25"><template #item.amount="{ item }"><span class="mono">{{ money(item.amount, '¥') }}</span></template></v-data-table></div>
          </section>
        </v-window-item>

        <v-window-item value="audit">
          <section v-if="admin.dead_letters?.length" class="section-band">
            <div class="section-band__head"><div><h2>Dead Letters</h2><p>未解决同步错误</p></div></div>
            <div class="section-band__body section-band__body--flush"><v-data-table :headers="[{title:'ID',key:'id'},{title:'源事件',key:'source_event_id'},{title:'错误',key:'error'},{title:'时间',key:'at'}]" :items="admin.dead_letters" :items-per-page="25" /></div>
          </section>
          <section class="section-band">
            <div class="section-band__head"><div><h2>最近审计</h2><p>操作、目标、操作者和原因</p></div></div>
            <div class="section-band__body section-band__body--flush"><v-data-table :headers="[{title:'操作',key:'operation'},{title:'目标',key:'target'},{title:'操作者',key:'operator'},{title:'原因',key:'reason'},{title:'时间',key:'at'}]" :items="admin.audits || []" :items-per-page="25" /></div>
          </section>
        </v-window-item>
      </v-window>
    </LoadingState>

    <v-dialog v-model="resetQuotaDialog.open" max-width="520">
      <v-card>
        <v-card-title>确认重置 CPA 本地额度状态</v-card-title>
        <v-card-text>
          <v-alert type="warning" variant="tonal" class="mb-4">该操作只清除 CPA 的 quota/cooldown 路由状态，不会充值或手动重置上游套餐额度。</v-alert>
          <div class="mb-3">账号：<strong>{{ resetQuotaDialog.account?.name || '-' }}</strong></div>
          <v-textarea v-model="resetQuotaDialog.reason" label="操作原因" rows="3" required />
        </v-card-text>
        <v-card-actions><v-spacer /><v-btn variant="text" @click="resetQuotaDialog.open = false">取消</v-btn><v-btn color="warning" :loading="mutating" :disabled="!resetQuotaDialog.reason.trim()" @click="resetQuota">确认重置</v-btn></v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="cycleDialog.open" max-width="760">
      <v-card><v-card-title>创建账期</v-card-title><v-card-text><div class="dialog-grid"><v-text-field v-model="cycleDialog.name" label="名称" /><v-select v-model="cycleDialog.gradient_rule_id" :items="activeGradients" item-title="name" item-value="id" label="梯度规则" /><v-text-field v-model="cycleDialog.start" label="开始时间" type="datetime-local" /><v-text-field v-model="cycleDialog.end" label="结束时间" type="datetime-local" /><div class="dialog-wide pool-cost-editor"><div class="editor-label">资源池固定成本（人民币）</div><v-text-field v-for="pool in activePools" :key="pool.id" v-model="cycleDialog.pool_costs[pool.id]" :label="pool.name" type="number" min="0" step="0.01" prefix="¥" /></div><v-textarea v-model="cycleDialog.waiver" label="数据质量说明" rows="2" class="dialog-wide" /></div></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="cycleDialog.open = false">取消</v-btn><v-btn color="primary" :loading="mutating" @click="createCycle"><Save :size="16" class="mr-2" />创建</v-btn></v-card-actions></v-card>
    </v-dialog>

    <v-dialog v-model="cycleConfigDialog.open" max-width="680">
      <v-card><v-card-title>配置账期 {{ cycleConfigDialog.cycle?.name }}</v-card-title><v-card-text><v-select v-model="cycleConfigDialog.gradient_rule_id" :items="activeGradients" item-title="name" item-value="id" label="梯度规则" /><div class="pool-cost-editor mt-4"><div class="editor-label">资源池固定成本（人民币）</div><v-text-field v-for="pool in activePools" :key="pool.id" v-model="cycleConfigDialog.pool_costs[pool.id]" :label="pool.name" type="number" min="0" step="0.01" prefix="¥" /></div><v-textarea v-model="cycleConfigDialog.reason" label="修改原因" rows="2" class="mt-4" /></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="cycleConfigDialog.open = false">取消</v-btn><v-btn color="primary" :loading="mutating" @click="saveCycleConfig"><Save :size="16" class="mr-2" />保存配置</v-btn></v-card-actions></v-card>
    </v-dialog>

    <v-dialog v-model="gradientDialog.open" max-width="820">
      <v-card><v-card-title>{{ gradientDialog.id ? '编辑梯度规则' : '新增梯度规则' }}</v-card-title><v-card-text><div class="dialog-grid"><v-text-field v-model="gradientDialog.name" label="规则名称" /><v-text-field v-model="gradientDialog.description" label="说明" /><div class="dialog-wide tier-editor"><div class="tier-editor__head"><div><strong>按序计费区间</strong><div class="data-muted text-caption">左边界自动衔接；最后一段上限固定为空</div></div><v-btn size="small" variant="outlined" @click="addTier"><Plus :size="15" class="mr-1" />增加区间</v-btn></div><div v-for="(tier, index) in gradientDialog.tiers" :key="index" class="tier-editor__row"><div class="tier-order mono">{{ index + 1 }}</div><v-text-field v-model="tier.left" label="左边界 USD" type="number" min="0" readonly /><v-text-field v-model="tier.right" label="右边界 USD" type="number" min="0" :disabled="index === gradientDialog.tiers.length - 1" :placeholder="index === gradientDialog.tiers.length - 1 ? '∞' : ''" @update:model-value="index + 1 < gradientDialog.tiers.length && (gradientDialog.tiers[index + 1].left = tier.right)" /><v-text-field v-model="tier.multiplier" label="倍率" type="number" min="0" step="0.01" suffix="x" /><div class="tier-actions"><v-tooltip text="上移"><template #activator="{ props }"><v-btn v-bind="props" icon variant="text" size="small" :disabled="index === 0" @click="moveTier(index, -1)"><ArrowUp :size="16" /></v-btn></template></v-tooltip><v-tooltip text="下移"><template #activator="{ props }"><v-btn v-bind="props" icon variant="text" size="small" :disabled="index === gradientDialog.tiers.length - 1" @click="moveTier(index, 1)"><ArrowDown :size="16" /></v-btn></template></v-tooltip><v-tooltip text="删除区间"><template #activator="{ props }"><v-btn v-bind="props" icon variant="text" size="small" color="error" :disabled="gradientDialog.tiers.length === 1" @click="removeTier(index)"><Trash2 :size="16" /></v-btn></template></v-tooltip></div></div></div><v-textarea v-model="gradientDialog.reason" label="变更原因" rows="2" class="dialog-wide" /></div></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="gradientDialog.open = false">取消</v-btn><v-btn color="primary" :loading="mutating" @click="saveGradient"><Save :size="16" class="mr-2" />保存规则</v-btn></v-card-actions></v-card>
    </v-dialog>

    <v-dialog v-model="deleteGradientDialog.open" max-width="520"><v-card><v-card-title>停用梯度规则</v-card-title><v-card-text><v-alert type="warning" variant="tonal" class="mb-4">仍被未关闭账期使用的规则不能停用。</v-alert><v-textarea v-model="deleteGradientDialog.reason" label="停用原因" rows="2" /></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="deleteGradientDialog.open = false">取消</v-btn><v-btn color="error" :loading="mutating" @click="deleteGradient"><Trash2 :size="16" class="mr-2" />停用</v-btn></v-card-actions></v-card></v-dialog>

    <v-dialog v-model="keyProfileDialog.open" max-width="560"><v-card><v-card-title>未绑定 Key 计费档案</v-card-title><v-card-text><v-alert type="info" variant="tonal" class="mb-4"><span class="mono">{{ keyProfileDialog.key?.masked }}</span><br>预估付费 = 本地等效成本 USD × 倍率，结果计入人民币资源池抵扣。</v-alert><v-text-field v-model="keyProfileDialog.name" label="显示别名" /><v-text-field v-model="keyProfileDialog.multiplier" label="倍率（人民币 / USD）" type="number" min="0" step="0.01" /><v-textarea v-model="keyProfileDialog.reason" label="修改原因（选填）" rows="2" /></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="keyProfileDialog.open = false">取消</v-btn><v-btn color="primary" :loading="mutating" @click="saveKeyProfile"><Save :size="16" class="mr-2" />保存</v-btn></v-card-actions></v-card></v-dialog>

    <v-dialog v-model="pricingDialog.open" max-width="560"><v-card><v-card-title>同步上游模型价格</v-card-title><v-card-text><v-alert type="warning" variant="tonal" class="mb-4">同步会创建新的上游价格版本，并提交未关闭账期后台重算。手动调整会被本次同步覆盖，关闭账期保持不变。</v-alert><v-text-field v-model="pricingDialog.name" label="版本名称（留空自动生成）" /><v-textarea v-model="pricingDialog.reason" label="同步原因" rows="2" /></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="pricingDialog.open = false">取消</v-btn><v-btn color="secondary" :loading="mutating" @click="syncPricing"><CloudDownload :size="16" class="mr-2" />同步并后台重算</v-btn></v-card-actions></v-card></v-dialog>

    <v-dialog v-model="pricingRuleDialog.open" max-width="980">
      <v-card>
        <v-card-title>{{ pricingRuleDialog.editing_model ? '手动调整模型计费规则' : '新增模型计费规则' }}</v-card-title>
        <v-card-text>
          <v-alert type="warning" variant="tonal" class="mb-4">
            单位均为 USD / 1M tokens。保存会生成新的 active 版本，并提交未关闭账期后台重算；价格版本名称留空时自动生成。
          </v-alert>
          <div class="dialog-grid">
            <v-text-field v-model="pricingRuleDialog.model" label="模型名称" :disabled="Boolean(pricingRuleDialog.editing_model)" class="dialog-wide" />
            <v-text-field v-model="pricingRuleDialog.input_usd_per_million" label="Default Input" type="number" min="0" step="0.001" prefix="$" />
            <v-text-field v-model="pricingRuleDialog.output_usd_per_million" label="Default Output" type="number" min="0" step="0.001" prefix="$" />
            <v-text-field v-model="pricingRuleDialog.cache_read_usd_per_million" label="Default Cache read" type="number" min="0" step="0.001" prefix="$" />
            <v-text-field v-model="pricingRuleDialog.cache_creation_usd_per_million" label="Default Cache creation" type="number" min="0" step="0.001" prefix="$" />
            <v-text-field v-model="pricingRuleDialog.priority_input_usd_per_million" label="Priority Input（空=Default）" type="number" min="0" step="0.001" prefix="$" />
            <v-text-field v-model="pricingRuleDialog.priority_output_usd_per_million" label="Priority Output（空=Default）" type="number" min="0" step="0.001" prefix="$" />
            <v-text-field v-model="pricingRuleDialog.priority_cache_read_usd_per_million" label="Priority Cache read（空=Default）" type="number" min="0" step="0.001" prefix="$" />
            <v-text-field v-model="pricingRuleDialog.priority_cache_creation_usd_per_million" label="Priority Cache creation（空=Default）" type="number" min="0" step="0.001" prefix="$" />
            <v-text-field v-model="pricingRuleDialog.flex_input_usd_per_million" label="Flex Input（空=Default）" type="number" min="0" step="0.001" prefix="$" />
            <v-text-field v-model="pricingRuleDialog.flex_output_usd_per_million" label="Flex Output（空=Default）" type="number" min="0" step="0.001" prefix="$" />
            <v-text-field v-model="pricingRuleDialog.long_context_threshold_tokens" label="长上下文阈值 tokens（空=不启用）" type="number" min="0" />
            <v-text-field v-model="pricingRuleDialog.long_context_input_multiplier" label="长上下文 Input 倍率" type="number" min="0" step="0.000001" suffix="x" />
            <v-text-field v-model="pricingRuleDialog.long_context_output_multiplier" label="长上下文 Output 倍率" type="number" min="0" step="0.000001" suffix="x" />
            <v-text-field v-model="pricingRuleDialog.version_name" label="价格版本名称（选填）" />
            <v-textarea v-model="pricingRuleDialog.reason" label="变更原因" rows="2" class="dialog-wide" />
          </div>
        </v-card-text>
        <v-card-actions><v-spacer /><v-btn variant="text" @click="pricingRuleDialog.open = false">取消</v-btn><v-btn color="primary" :loading="mutating" :disabled="!pricingRuleDialog.model.trim() || !pricingRuleDialog.input_usd_per_million || !pricingRuleDialog.output_usd_per_million || !pricingRuleDialog.cache_read_usd_per_million || !pricingRuleDialog.cache_creation_usd_per_million || !pricingRuleDialog.reason.trim()" @click="savePricingRule"><Save :size="16" class="mr-2" />保存并后台重算</v-btn></v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="closeDialog.open" max-width="520"><v-card><v-card-title>关闭账期 {{ closeDialog.cycle?.name }}</v-card-title><v-card-text><v-alert type="error" variant="tonal" border="start" class="mb-4">关闭后价格版本、梯度规则和账单金额被冻结且不可修改。</v-alert><v-checkbox v-model="closeDialog.confirm_close" label="确认冻结账单" /><v-checkbox v-if="closeDialog.cycle?.waiver" v-model="closeDialog.confirm_waiver" label="确认数据质量说明" /></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="closeDialog.open = false">取消</v-btn><v-btn color="error" :loading="mutating" @click="closeCycle">关闭账期</v-btn></v-card-actions></v-card></v-dialog>

    <v-dialog v-model="adjustmentDialog.open" max-width="560"><v-card><v-card-title>调整最终费用</v-card-title><v-card-text><v-select v-model="adjustmentDialog.cycle" :items="admin.cycles || []" item-title="name" item-value="name" label="账期" /><v-text-field v-model="adjustmentDialog.telegram_user_id" label="Telegram 用户 ID" type="number" /><v-text-field v-model="adjustmentDialog.amount_cents" label="最终应付调整（人民币分，可为负数）" type="number" /><v-textarea v-model="adjustmentDialog.reason" label="原因" rows="2" /></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="adjustmentDialog.open = false">取消</v-btn><v-btn color="primary" :loading="mutating" @click="createAdjustment">添加</v-btn></v-card-actions></v-card></v-dialog>

    <v-dialog v-model="manualUsageDialog.open" max-width="620">
      <v-card>
        <v-card-title>{{ manualUsageDialog.id ? '编辑原始等效用量' : '添加原始等效用量' }}</v-card-title>
        <v-card-text>
          <v-alert type="info" variant="tonal" class="mb-4">
            所有业务字段均可更新，保存后会重新计算原账期和目标账期。不会生成请求、Token 或 CPAMP 事件；负数不能使补录余额低于零。
          </v-alert>
          <div class="dialog-grid">
            <v-select :model-value="manualUsageDialog.cycle" :items="openCycles" item-title="name" item-value="name" label="未关闭账期" @update:model-value="selectManualUsageCycle" />
            <v-select v-model="manualUsageDialog.pool_id" :items="manualUsagePools" item-title="pool" item-value="pool_id" label="资源池" />
            <v-autocomplete v-model="manualUsageDialog.telegram_user_id" :items="registeredUsers" item-title="name" item-value="id" label="Telegram 用户" />
            <v-text-field v-model="manualUsageDialog.amount_usd" label="原始等效用量（USD）" type="number" step="0.000000001" prefix="$" />
            <v-textarea v-model="manualUsageDialog.reason" label="原因" rows="2" class="dialog-wide" />
          </div>
        </v-card-text>
        <v-card-actions><v-spacer /><v-btn variant="text" @click="manualUsageDialog.open = false">取消</v-btn><v-btn color="primary" :loading="mutating" :disabled="!manualUsageDialog.cycle || !manualUsageDialog.pool_id || !manualUsageDialog.telegram_user_id || !manualUsageDialog.amount_usd || !manualUsageDialog.reason.trim()" @click="saveManualUsage"><Save :size="16" class="mr-2" />{{ manualUsageDialog.id ? '保存修改' : '计入账期' }}</v-btn></v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="transferDialog.open" max-width="560"><v-card><v-card-title>变更 Key 归属</v-card-title><v-card-text><v-text-field v-model="transferDialog.key_id" label="Key ID" type="number" min="1" /><v-text-field v-model="transferDialog.telegram_user_id" label="新 Telegram 用户 ID" type="number" /><v-textarea v-model="transferDialog.reason" label="原因" rows="2" /><v-checkbox v-model="transferDialog.confirm_transfer" label="确认默认仅影响未来用量" /></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="transferDialog.open = false">取消</v-btn><v-btn color="error" :loading="mutating" @click="transferOwnership">执行</v-btn></v-card-actions></v-card></v-dialog>

    <v-dialog v-model="poolDialog.open" max-width="560"><v-card><v-card-title>创建资源池</v-card-title><v-card-text><v-text-field v-model="poolDialog.name" label="名称" /><v-text-field v-model="poolDialog.auth_pattern" label="Auth index 正则" /><v-text-field v-model="poolDialog.model_pattern" label="模型正则" /><v-text-field v-model="poolDialog.priority" label="优先级" type="number" /></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="poolDialog.open = false">取消</v-btn><v-btn color="primary" :loading="mutating" @click="createPool">创建</v-btn></v-card-actions></v-card></v-dialog>

    <v-dialog v-model="userAdminDialog.open" max-width="560">
      <v-card>
        <v-card-title>{{ userAdminDialog.is_admin ? '授予 Web 管理权限' : '撤销 Web 管理权限' }}</v-card-title>
        <v-card-text>
          <v-alert type="info" variant="tonal" class="mb-4">目标用户：{{ userAdminDialog.user?.name }}（{{ userAdminDialog.user?.id }}）</v-alert>
          <v-textarea v-model="userAdminDialog.reason" label="变更原因" rows="3" autofocus />
        </v-card-text>
        <v-card-actions><v-spacer /><v-btn variant="text" @click="userAdminDialog.open = false">取消</v-btn><v-btn color="primary" :disabled="!userAdminDialog.reason.trim()" :loading="mutating" @click="saveUserAdmin">确认变更</v-btn></v-card-actions>
      </v-card>
    </v-dialog>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="4500">{{ snackbar.text }}</v-snackbar>
  </div>
</template>

<style scoped>
.admin-tabs { background: #fff; border: 1px solid #d8dfdc; border-radius: 6px; }
.admin-wrap { max-width: 320px; white-space: normal; overflow-wrap: anywhere; }
.admin-actions { white-space: nowrap; }
.dialog-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.dialog-wide { grid-column: 1 / -1; }
.pool-cost-editor { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; padding: 14px; border: 1px solid #d8dfdc; background: #f6f8f7; }
.editor-label { grid-column: 1 / -1; color: #46504d; font-size: .78rem; font-weight: 700; }
.tier-summary { max-width: 540px; white-space: normal; }
.tier-summary span { display: inline-block; margin: 2px 10px 2px 0; }
.tier-editor { border: 1px solid #cfd8d4; background: #f6f8f7; }
.tier-editor__head { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 12px 14px; border-bottom: 1px solid #d8dfdc; background: #eef3f1; color: #202427; }
.tier-editor__row { display: grid; grid-template-columns: 34px repeat(3, minmax(0, 1fr)) 108px; gap: 10px; align-items: start; padding: 12px 14px; border-bottom: 1px solid #dfe5e2; background: #fff; color: #202427; }
.tier-editor__row:last-child { border-bottom: 0; }
.tier-order { width: 28px; height: 28px; display: grid; place-items: center; border: 1px solid #aebbb7; background: #eef3f1; color: #202427; border-radius: 4px; }
.tier-actions { display: flex; align-items: center; justify-content: end; padding-top: 2px; }
@media (max-width: 900px) { .tier-editor__row { grid-template-columns: 34px repeat(2, minmax(0, 1fr)); } .tier-actions { grid-column: 2 / -1; justify-content: start; } }
@media (max-width: 650px) { .dialog-grid, .pool-cost-editor { grid-template-columns: 1fr; } .dialog-wide, .editor-label { grid-column: auto; } .tier-editor__head { align-items: stretch; flex-direction: column; } .tier-editor__row { grid-template-columns: 30px 1fr; } .tier-actions { grid-column: 2; } }
</style>
