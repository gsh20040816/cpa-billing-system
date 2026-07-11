<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { Plus, RefreshCw, Scale, Shuffle, XCircle } from '@lucide/vue'
import { api } from '../api'
import LoadingState from '../components/LoadingState.vue'
import MetricRail from '../components/MetricRail.vue'
import PageHeader from '../components/PageHeader.vue'
import { money, number } from '../lib/format'

const loading = ref(true)
const mutating = ref(false)
const error = ref('')
const data = ref(null)
const tab = ref('overview')
const snackbar = reactive({ show: false, text: '', color: 'success' })

const cycleDialog = reactive({ open: false, name: '', start: '', end: '', fixed_cost: '', waiver: '' })
const closeDialog = reactive({ open: false, cycle: null, confirm_close: false, confirm_waiver: false })
const adjustmentDialog = reactive({ open: false, cycle: '', telegram_user_id: '', amount_cents: '', reason: '' })
const transferDialog = reactive({ open: false, key_id: '', telegram_user_id: '', reason: '', confirm_transfer: false })
const poolDialog = reactive({ open: false, name: '', auth_pattern: '', model_pattern: '', priority: 100 })
const pricingDialog = reactive({ open: false, name: '', confirm_import: false })

const reconciliation = computed(() => data.value?.reconciliation || {})
const admin = computed(() => data.value?.admin || {})
const metrics = computed(() => [
  { label: 'CPAMP 事件', value: number(reconciliation.value.cpamp_events), mono: true },
  { label: '镜像事件', value: number(reconciliation.value.raw_events), mono: true },
  { label: '已计价', value: number(reconciliation.value.rated_events), mono: true },
  { label: '24h 请求', value: number(data.value?.usage?.recent_requests), mono: true },
  { label: '24h Tokens', value: number(data.value?.usage?.recent_tokens), mono: true },
  { label: '24h 等效成本', value: money(data.value?.usage?.recent_cost), mono: true },
])

function notify(text, color = 'success') {
  snackbar.text = text
  snackbar.color = color
  snackbar.show = true
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api('/api/admin/snapshot', { admin: true })
  } catch (exc) {
    error.value = exc.message
  } finally {
    loading.value = false
  }
}

async function mutate(path, body, success) {
  mutating.value = true
  try {
    await api(path, { admin: true, body })
    notify(success)
    await load()
    return true
  } catch (exc) {
    notify(exc.message, 'error')
    return false
  } finally {
    mutating.value = false
  }
}

async function createCycle() {
  if (await mutate('/api/admin/cycles', { ...cycleDialog }, '账期已创建')) cycleDialog.open = false
}

async function previewCycle(name) {
  await mutate(`/api/admin/cycles/${encodeURIComponent(name)}/preview`, {}, '账单预览已更新')
}

function openClose(cycle) {
  closeDialog.cycle = cycle
  closeDialog.confirm_close = false
  closeDialog.confirm_waiver = false
  closeDialog.open = true
}

async function closeCycle() {
  if (await mutate(`/api/admin/cycles/${encodeURIComponent(closeDialog.cycle.name)}/close`, {
    confirm_close: closeDialog.confirm_close,
    confirm_waiver: closeDialog.confirm_waiver,
  }, '账期已关闭并冻结')) closeDialog.open = false
}

async function createAdjustment() {
  if (await mutate('/api/admin/adjustments', {
    cycle: adjustmentDialog.cycle,
    telegram_user_id: Number(adjustmentDialog.telegram_user_id),
    amount_cents: Number(adjustmentDialog.amount_cents),
    reason: adjustmentDialog.reason,
  }, '人工调整已添加')) adjustmentDialog.open = false
}

async function transferOwnership() {
  if (await mutate('/api/admin/ownership-transfers', {
    key_id: Number(transferDialog.key_id),
    telegram_user_id: Number(transferDialog.telegram_user_id),
    reason: transferDialog.reason,
    confirm_transfer: transferDialog.confirm_transfer,
  }, 'Key 归属已变更')) transferDialog.open = false
}

async function createPool() {
  if (await mutate('/api/admin/pools', {
    name: poolDialog.name,
    auth_pattern: poolDialog.auth_pattern || null,
    model_pattern: poolDialog.model_pattern || null,
    priority: Number(poolDialog.priority),
  }, '资源池已创建')) poolDialog.open = false
}

async function importPricing() {
  if (await mutate('/api/admin/pricing-versions/import', {
    name: pricingDialog.name,
    confirm_import: pricingDialog.confirm_import,
  }, '价格版本已导入')) pricingDialog.open = false
}

onMounted(load)
</script>

<template>
  <div class="content-shell content-shell--admin">
    <PageHeader eyebrow="Independent admin session" title="系统管理" subtitle="同步、计价、归属与结算">
      <template #actions>
        <v-tooltip text="刷新管理数据"><template #activator="{ props }"><v-btn v-bind="props" icon variant="outlined" :loading="loading" @click="load"><RefreshCw :size="18" /></v-btn></template></v-tooltip>
      </template>
    </PageHeader>

    <LoadingState :loading="loading" :error="error" :empty="!data" @retry="load">
      <v-alert v-if="!reconciliation.ok" type="error" variant="tonal" border="start" class="mb-4">
        对账存在阻断项：Dead letter {{ number(reconciliation.dead_letters) }}，未归属 {{ number(reconciliation.unowned_events) }}，未分池 {{ number(reconciliation.unassigned_events) }}。
      </v-alert>
      <MetricRail :items="metrics" :columns="6" />

      <v-tabs v-model="tab" color="primary" class="admin-tabs" show-arrows>
        <v-tab value="overview">运行概览</v-tab>
        <v-tab value="cycles">账期</v-tab>
        <v-tab value="identity">用户与 Keys</v-tab>
        <v-tab value="rules">资源池与价格</v-tab>
        <v-tab value="adjustments">调整与归属</v-tab>
        <v-tab value="audit">审计</v-tab>
      </v-tabs>

      <v-window v-model="tab" class="mt-4">
        <v-window-item value="overview">
          <section class="section-band">
            <div class="section-band__head"><div><h2>同步状态</h2><p>Worker checkpoint</p></div></div>
            <div class="section-band__body section-band__body--flush">
              <v-table density="compact"><thead><tr><th>来源</th><th>Last ID</th><th>最后事件</th><th>最后成功</th><th class="text-right">Backlog</th><th>错误</th></tr></thead><tbody><tr v-for="item in admin.sync || []" :key="item.source"><td>{{ item.source }}</td><td class="mono">{{ number(item.last_event_id) }}</td><td>{{ item.last_event_at }}</td><td>{{ item.last_success_at }}</td><td class="text-right mono">{{ number(item.backlog) }}</td><td :class="item.last_error ? 'data-error' : 'data-muted'">{{ item.last_error || '-' }}</td></tr></tbody></v-table>
            </div>
          </section>
          <section class="section-band">
            <div class="section-band__head"><div><h2>上游账号</h2><p>Keeper 额度缓存</p></div></div>
            <div class="section-band__body section-band__body--flush">
              <v-table density="compact"><thead><tr><th>账号</th><th>类型</th><th>计划</th><th class="text-right">请求</th><th class="text-right">Tokens</th><th>额度</th></tr></thead><tbody><tr v-for="item in data?.accounts?.accounts || []" :key="item.id"><td>{{ item.name }}</td><td>{{ item.type }}</td><td>{{ item.plan_type }}</td><td class="text-right mono">{{ number(item.usage.requests) }}</td><td class="text-right mono">{{ number(item.usage.total_tokens) }}</td><td>{{ item.quota.map(q => `${q.label} ${q.used_percent}%`).join(' · ') || '-' }}</td></tr></tbody></v-table>
            </div>
          </section>
        </v-window-item>

        <v-window-item value="cycles">
          <section class="section-band">
            <div class="section-band__head"><div><h2>账期</h2><p>Closed 后不可修改</p></div><v-btn color="primary" size="small" @click="cycleDialog.open = true"><Plus :size="16" class="mr-2" />创建账期</v-btn></div>
            <div class="section-band__body section-band__body--flush">
              <v-table density="compact"><thead><tr><th>名称</th><th>开始</th><th>结束</th><th>固定成本</th><th>状态</th><th>数据说明</th><th class="text-right">操作</th></tr></thead><tbody><tr v-for="item in admin.cycles || []" :key="item.name"><td class="mono">{{ item.name }}</td><td>{{ item.start }}</td><td>{{ item.end }}</td><td class="mono">{{ money(item.fixed_cost, '¥') }}</td><td><v-chip :color="item.status === 'closed' ? 'default' : 'primary'" variant="tonal">{{ item.status }}</v-chip></td><td class="admin-wrap">{{ item.waiver || '-' }}</td><td class="text-right"><template v-if="item.status !== 'closed'"><v-btn size="small" variant="text" @click="previewCycle(item.name)"><Scale :size="15" class="mr-1" />预览</v-btn><v-btn size="small" variant="text" color="error" @click="openClose(item)"><XCircle :size="15" class="mr-1" />关闭</v-btn></template></td></tr></tbody></v-table>
            </div>
          </section>
        </v-window-item>

        <v-window-item value="identity">
          <section class="section-band">
            <div class="section-band__head"><div><h2>Telegram 用户</h2><p>注册与手动授权状态</p></div></div>
            <div class="section-band__body section-band__body--flush"><v-data-table :headers="[{title:'ID',key:'id'},{title:'用户',key:'name'},{title:'已注册',key:'registered'},{title:'手动授权',key:'manual_allowed'},{title:'有效 Keys',key:'active_keys'}]" :items="admin.users || []" :items-per-page="25" /></div>
          </section>
          <section class="section-band">
            <div class="section-band__head"><div><h2>全部 API Keys</h2><p>仅显示掩码</p></div></div>
            <div class="section-band__body section-band__body--flush"><v-data-table :headers="[{title:'ID',key:'id'},{title:'Key',key:'masked'},{title:'名称',key:'name'},{title:'状态',key:'status'},{title:'归属',key:'owner'},{title:'创建',key:'created_at'},{title:'吊销',key:'revoked_at'}]" :items="admin.keys || []" :items-per-page="50"><template #item.masked="{ item }"><span class="mono">{{ item.masked }}</span></template></v-data-table></div>
          </section>
          <section class="section-band">
            <div class="section-band__head"><div><h2>归属历史</h2><p>最近 100 条</p></div></div>
            <div class="section-band__body section-band__body--flush"><v-data-table :headers="[{title:'Key',key:'key'},{title:'用户',key:'user'},{title:'开始',key:'from'},{title:'结束',key:'to'},{title:'来源',key:'source'},{title:'原因',key:'reason'}]" :items="admin.ownership || []" :items-per-page="25"><template #item.key="{ item }"><span class="mono">#{{ item.key_id }} {{ item.key }}</span></template></v-data-table></div>
          </section>
        </v-window-item>

        <v-window-item value="rules">
          <div class="two-column">
            <section class="section-band">
              <div class="section-band__head"><div><h2>资源池</h2><p>事件归属规则</p></div><v-btn size="small" color="primary" @click="poolDialog.open = true"><Plus :size="16" class="mr-2" />创建</v-btn></div>
              <div class="section-band__body"><v-list density="compact" border><v-list-item v-for="item in admin.pools || []" :key="item.id" :title="item.name" :subtitle="item.active ? 'active' : 'inactive'" /></v-list></div>
            </section>
            <section class="section-band">
              <div class="section-band__head"><div><h2>价格版本</h2><p>新事件使用活动版本</p></div><v-btn size="small" color="secondary" @click="pricingDialog.open = true"><Plus :size="16" class="mr-2" />导入</v-btn></div>
              <div class="section-band__body"><v-list density="compact" border><v-list-item v-for="item in admin.pricing || []" :key="item.id" :title="item.name" :subtitle="`${item.status} · ${item.source}`" /></v-list></div>
            </section>
          </div>
        </v-window-item>

        <v-window-item value="adjustments">
          <div class="d-flex ga-2 mb-4"><v-btn color="primary" @click="adjustmentDialog.open = true"><Scale :size="16" class="mr-2" />人工调整</v-btn><v-btn color="secondary" @click="transferDialog.open = true"><Shuffle :size="16" class="mr-2" />变更归属</v-btn></div>
          <section class="section-band">
            <div class="section-band__head"><div><h2>最近人工调整</h2><p>最近 100 条</p></div></div>
            <div class="section-band__body section-band__body--flush"><v-data-table :headers="[{title:'账期',key:'cycle'},{title:'用户',key:'user_id'},{title:'金额',key:'amount'},{title:'原因',key:'reason'},{title:'时间',key:'at'}]" :items="admin.adjustments || []" :items-per-page="25"><template #item.amount="{ item }"><span class="mono">{{ money(item.amount, '¥') }}</span></template></v-data-table></div>
          </section>
        </v-window-item>

        <v-window-item value="audit">
          <section v-if="admin.dead_letters?.length" class="section-band">
            <div class="section-band__head"><div><h2>Dead Letters</h2><p>未解决同步错误</p></div></div>
            <div class="section-band__body section-band__body--flush"><v-data-table :headers="[{title:'ID',key:'id'},{title:'源事件',key:'source_event_id'},{title:'错误',key:'error'},{title:'时间',key:'at'}]" :items="admin.dead_letters" :items-per-page="25" /></div>
          </section>
          <section class="section-band">
            <div class="section-band__head"><div><h2>最近审计</h2><p>操作、目标与操作者</p></div></div>
            <div class="section-band__body section-band__body--flush"><v-data-table :headers="[{title:'操作',key:'operation'},{title:'目标',key:'target'},{title:'操作者',key:'operator'},{title:'原因',key:'reason'},{title:'时间',key:'at'}]" :items="admin.audits || []" :items-per-page="25" /></div>
          </section>
        </v-window-item>
      </v-window>
    </LoadingState>

    <v-dialog v-model="cycleDialog.open" max-width="680"><v-card><v-card-title>创建账期</v-card-title><v-card-text><div class="dialog-grid"><v-text-field v-model="cycleDialog.name" label="名称" /><v-text-field v-model="cycleDialog.fixed_cost" label="固定成本（元）" type="number" min="0" step="0.01" /><v-text-field v-model="cycleDialog.start" label="开始时间" type="datetime-local" /><v-text-field v-model="cycleDialog.end" label="结束时间" type="datetime-local" /><v-textarea v-model="cycleDialog.waiver" label="数据质量说明" rows="2" class="dialog-wide" /></div></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="cycleDialog.open = false">取消</v-btn><v-btn color="primary" :loading="mutating" @click="createCycle">创建</v-btn></v-card-actions></v-card></v-dialog>

    <v-dialog v-model="closeDialog.open" max-width="520"><v-card><v-card-title>关闭账期 {{ closeDialog.cycle?.name }}</v-card-title><v-card-text><v-alert type="error" variant="tonal" border="start" class="mb-4">关闭后账单被冻结且不可修改。</v-alert><v-checkbox v-model="closeDialog.confirm_close" label="确认冻结账单" /><v-checkbox v-if="closeDialog.cycle?.waiver" v-model="closeDialog.confirm_waiver" label="确认数据质量说明" /></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="closeDialog.open = false">取消</v-btn><v-btn color="error" :loading="mutating" @click="closeCycle">关闭账期</v-btn></v-card-actions></v-card></v-dialog>

    <v-dialog v-model="adjustmentDialog.open" max-width="560"><v-card><v-card-title>人工调整</v-card-title><v-card-text><v-select v-model="adjustmentDialog.cycle" :items="admin.cycles || []" item-title="name" item-value="name" label="账期" /><v-text-field v-model="adjustmentDialog.telegram_user_id" label="Telegram 用户 ID" type="number" /><v-text-field v-model="adjustmentDialog.amount_cents" label="金额（分，可为负数）" type="number" /><v-textarea v-model="adjustmentDialog.reason" label="原因" rows="2" /></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="adjustmentDialog.open = false">取消</v-btn><v-btn color="primary" :loading="mutating" @click="createAdjustment">添加</v-btn></v-card-actions></v-card></v-dialog>

    <v-dialog v-model="transferDialog.open" max-width="560"><v-card><v-card-title>变更 Key 归属</v-card-title><v-card-text><v-text-field v-model="transferDialog.key_id" label="Key ID" type="number" min="1" /><v-text-field v-model="transferDialog.telegram_user_id" label="新 Telegram 用户 ID" type="number" /><v-textarea v-model="transferDialog.reason" label="原因" rows="2" /><v-checkbox v-model="transferDialog.confirm_transfer" label="确认默认仅影响未来用量" /></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="transferDialog.open = false">取消</v-btn><v-btn color="error" :loading="mutating" @click="transferOwnership">执行</v-btn></v-card-actions></v-card></v-dialog>

    <v-dialog v-model="poolDialog.open" max-width="560"><v-card><v-card-title>创建资源池</v-card-title><v-card-text><v-text-field v-model="poolDialog.name" label="名称" /><v-text-field v-model="poolDialog.auth_pattern" label="Auth index 正则" /><v-text-field v-model="poolDialog.model_pattern" label="模型正则" /><v-text-field v-model="poolDialog.priority" label="优先级" type="number" /></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="poolDialog.open = false">取消</v-btn><v-btn color="primary" :loading="mutating" @click="createPool">创建</v-btn></v-card-actions></v-card></v-dialog>

    <v-dialog v-model="pricingDialog.open" max-width="520"><v-card><v-card-title>导入价格版本</v-card-title><v-card-text><v-text-field v-model="pricingDialog.name" label="版本名称" /><v-checkbox v-model="pricingDialog.confirm_import" label="确认新事件切换到该价格版本" /></v-card-text><v-card-actions><v-spacer /><v-btn variant="text" @click="pricingDialog.open = false">取消</v-btn><v-btn color="secondary" :loading="mutating" @click="importPricing">导入</v-btn></v-card-actions></v-card></v-dialog>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="4500">{{ snackbar.text }}</v-snackbar>
  </div>
</template>

<style scoped>
.admin-tabs { background: #fff; border: 1px solid #d8dfdc; border-radius: 6px; }
.admin-wrap { max-width: 320px; white-space: normal; }
.dialog-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.dialog-wide { grid-column: 1 / -1; }
@media (max-width: 650px) { .dialog-grid { grid-template-columns: 1fr; } .dialog-wide { grid-column: auto; } }
</style>
