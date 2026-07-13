<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { RefreshCw, ServerCog } from '@lucide/vue'
import { api } from '../api'
import LoadingState from '../components/LoadingState.vue'
import MetricRail from '../components/MetricRail.vue'
import PageHeader from '../components/PageHeader.vue'
import { dateTime, money, number, percent, quotaTone } from '../lib/format'

const loading = ref(true)
const refreshingAll = ref(false)
const error = ref('')
const data = ref(null)
const refreshing = reactive({})
const timers = new Set()
const snackbar = reactive({ show: false, text: '', color: 'success' })

const metrics = computed(() => {
  const inspection = data.value?.inspection || {}
  return [
    { label: '上游账号', value: number(inspection.total), mono: true },
    { label: '额度正常', value: number(inspection.normal), mono: true },
    { label: '达到限制', value: number(inspection.limit_reached), mono: true },
    { label: '认证异常', value: number(inspection.unauthorized_401_402), mono: true },
    { label: '其他失败', value: number(inspection.other_failed), mono: true },
    { label: '缓存完成', value: number(inspection.cached), mono: true },
  ]
})

function notify(text, color = 'success') {
  snackbar.text = text
  snackbar.color = color
  snackbar.show = true
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api('/api/site/accounts')
  } catch (exc) {
    error.value = exc.message
  } finally {
    loading.value = false
  }
}

function delay(ms) {
  return new Promise((resolve) => {
    const id = window.setTimeout(() => {
      timers.delete(id)
      resolve()
    }, ms)
    timers.add(id)
  })
}

async function poll(accountId) {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await delay(2000)
    try {
      const result = await api(`/api/site/accounts/${encodeURIComponent(accountId)}/refresh`)
      if (result.status === 'completed') {
        notify('额度已刷新')
        await load()
        return
      }
      if (result.status === 'failed') {
        notify('额度刷新失败', 'error')
        return
      }
    } catch (exc) {
      if (exc.status !== 404) {
        notify(exc.message, 'error')
        return
      }
    }
  }
  notify('额度刷新仍在执行，请稍后刷新页面', 'warning')
}

async function refreshAccounts(accountIds = []) {
  const all = accountIds.length === 0
  if (all) refreshingAll.value = true
  accountIds.forEach((id) => { refreshing[id] = true })
  try {
    const result = await api('/api/site/accounts/refresh', { body: { account_ids: accountIds } })
    if (!result.tasks.length && result.rejected.length) {
      notify('Keeper 未接受额度刷新', 'error')
      return
    }
    notify(`已提交 ${result.accepted} 个额度刷新任务`, 'info')
    await Promise.all(result.tasks.map((task) => poll(task.account_id)))
  } catch (exc) {
    notify(exc.message, 'error')
  } finally {
    if (all) refreshingAll.value = false
    accountIds.forEach((id) => { refreshing[id] = false })
  }
}

onMounted(load)
onBeforeUnmount(() => timers.forEach((id) => window.clearTimeout(id)))
</script>

<template>
  <div class="content-shell">
    <PageHeader title="上游账号" subtitle="额度百分比来自 Keeper；请求、Token 与费用由本面板按 CPAMP 事件计算">
      <template #actions>
        <v-btn color="primary" :loading="refreshingAll" @click="refreshAccounts()">
          <RefreshCw :size="17" class="mr-2" />刷新全部额度
        </v-btn>
      </template>
    </PageHeader>

    <LoadingState :loading="loading" :error="error" :empty="!data?.accounts?.length" empty-text="Keeper 暂无上游账号" @retry="load">
      <MetricRail :items="metrics" :columns="6" />
      <div class="account-list">
        <v-card v-for="account in data?.accounts || []" :key="account.id" border class="account-card">
          <v-card-title class="account-card__head">
            <div class="d-flex align-center ga-3 min-w-0">
              <v-avatar color="surface-variant" rounded="sm"><ServerCog :size="20" /></v-avatar>
              <div class="min-w-0">
                <div class="account-name">{{ account.name }}</div>
                <div class="data-muted text-caption">{{ account.type }} · {{ account.plan_type || '未知计划' }} · {{ account.auth_type }}</div>
              </div>
            </div>
            <div class="d-flex align-center ga-2">
              <v-chip :color="account.disabled ? 'error' : account.quota_status === 'completed' ? 'success' : 'warning'" variant="tonal">
                {{ account.disabled ? '已停用' : account.quota_status }}
              </v-chip>
              <v-tooltip text="刷新该账号额度">
                <template #activator="{ props }">
                  <v-btn v-bind="props" icon variant="outlined" size="small" :disabled="!account.can_refresh" :loading="refreshing[account.id]" @click="refreshAccounts([account.id])">
                    <RefreshCw :size="17" />
                  </v-btn>
                </template>
              </v-tooltip>
            </div>
          </v-card-title>
          <v-divider />
          <v-card-text>
            <div class="account-usage">
              <div><span>历史请求</span><strong class="mono">{{ number(account.usage.requests) }}</strong></div>
              <div><span>历史 Tokens</span><strong class="mono">{{ number(account.usage.total_tokens) }}</strong></div>
              <div><span>历史成功率</span><strong class="mono">{{ percent(account.usage.success_rate, 2) }}</strong></div>
              <div><span>历史等效成本</span><strong class="mono">{{ money(account.usage.cost) }}</strong></div>
              <div><span>最后使用</span><strong>{{ dateTime(account.usage.last_used_at) }}</strong></div>
            </div>
            <v-alert v-if="account.usage.unpriced" type="warning" variant="tonal" density="compact" class="mt-3">
              仍有 {{ number(account.usage.unpriced) }} 条请求未匹配价格，当前费用为已计价部分。
            </v-alert>
            <div v-if="account.quota.length" class="mt-3">
              <div v-for="quota in account.quota" :key="quota.key" class="quota-row">
                <div>
                  <div class="quota-row__label">{{ quota.label }}</div>
                  <div class="quota-row__meta">{{ quota.plan_type || account.plan_type }}</div>
                </div>
                <div>
                  <div class="d-flex justify-space-between text-caption mb-1"><span>已使用</span><strong class="mono">{{ percent(quota.used_percent, 0) }}</strong></div>
                  <v-progress-linear :model-value="quota.used_percent || 0" :color="quotaTone(quota.used_percent)" height="8" rounded="sm" />
                </div>
                <div class="quota-row__meta">
                  <div v-if="quota.usage_filter?.mode === 'only_model'">模型范围：仅 {{ (quota.usage_filter.display_models || quota.usage_filter.models).join('、') }}</div>
                  <div v-else-if="quota.usage_filter?.mode === 'all_except_models'">模型范围：普通用量（排除 {{ (quota.usage_filter.display_models || quota.usage_filter.models).join('、') }}）</div>
                  <div v-else>模型范围：全部模型</div>
                  <div>统计自 {{ dateTime(quota.window_started_at) }}</div>
                  <div>重置 {{ dateTime(quota.reset_at) }}</div>
                  <div>{{ number(quota.window_usage_requests) }} 请求 · {{ number(quota.window_usage_tokens) }} tokens</div>
                  <div>{{ money(quota.window_usage_cost) }} 本窗口等效成本</div>
                  <div v-if="quota.window_unpriced" class="data-error">{{ number(quota.window_unpriced) }} 条未计价</div>
                </div>
              </div>
            </div>
            <v-alert v-else type="warning" variant="tonal" density="compact" class="mt-3">暂无额度缓存</v-alert>
            <div class="account-foot">
              <span>额度刷新 {{ dateTime(account.quota_refreshed_at) }}</span>
              <span v-if="account.reset_credits_available !== null">额度恢复积分：{{ account.reset_credits_available }}</span>
            </div>
          </v-card-text>
        </v-card>
      </div>
    </LoadingState>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="4500">{{ snackbar.text }}</v-snackbar>
  </div>
</template>

<style scoped>
.account-list { display: grid; gap: 14px; }
.account-card__head { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 14px 16px; }
.account-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: .98rem; font-weight: 700; }
.min-w-0 { min-width: 0; }
.account-usage { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); border: 1px solid #e1e7e4; }
.account-usage > div { min-width: 0; padding: 12px; border-right: 1px solid #e1e7e4; }
.account-usage > div:last-child { border-right: 0; }
.account-usage span { display: block; color: #68716e; font-size: .72rem; }
.account-usage strong { display: block; margin-top: 5px; overflow-wrap: anywhere; }
.account-foot { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-top: 12px; color: #717a77; font-size: .72rem; }
@media (max-width: 1000px) { .account-usage { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 760px) { .account-card__head { align-items: start; } .account-usage { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
