<script setup>
import { computed, inject, onMounted, reactive, ref } from 'vue'
import { Copy, KeyRound, Pencil, Plus, RefreshCw, RotateCw, Trash2 } from '@lucide/vue'
import { api, setCsrf } from '../api'
import LoadingState from '../components/LoadingState.vue'
import PageHeader from '../components/PageHeader.vue'
import { dateTime } from '../lib/format'

const userSession = inject('userSession')
const loading = ref(true)
const error = ref('')
const data = ref(null)
const actionLoading = ref(false)
const actionDialog = reactive({ open: false, action: 'add', target: null, currentKey: '' })
const renameDialog = reactive({ open: false, key: null, name: '' })
const revealDialog = reactive({ open: false, apiKey: '', action: '' })
const snackbar = reactive({ show: false, text: '', color: 'success' })

const headers = [
  { title: 'ID', key: 'id', width: 70 },
  { title: 'API Key', key: 'masked', minWidth: 190 },
  { title: '名称', key: 'name', minWidth: 170 },
  { title: '状态', key: 'status', width: 100 },
  { title: '创建时间', key: 'created_at', minWidth: 170 },
  { title: '吊销时间', key: 'revoked_at', minWidth: 170 },
  { title: '操作', key: 'actions', align: 'end', sortable: false, width: 150 },
]

const dialogTitle = computed(() => {
  if (actionDialog.action === 'add') return '新增 API Key'
  if (actionDialog.action === 'reset') return `重置 ${actionDialog.target?.masked || ''}`
  return `吊销 ${actionDialog.target?.masked || ''}`
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
    data.value = await api('/api/me/keys')
  } catch (exc) {
    error.value = exc.message
  } finally {
    loading.value = false
  }
}

function openAction(action, target = null) {
  actionDialog.action = action
  actionDialog.target = target
  actionDialog.currentKey = ''
  actionDialog.open = true
}

async function executeAction() {
  actionLoading.value = true
  try {
    const result = await api('/api/me/keys/actions', {
      body: {
        action: actionDialog.action,
        current_api_key: actionDialog.currentKey,
        target_key_id: actionDialog.target?.id || null,
      },
    })
    actionDialog.open = false
    if (result.csrf_token) {
      setCsrf(result.csrf_token)
      setCsrf(result.csrf_token, true)
    }
    if (result.new_api_key) {
      revealDialog.apiKey = result.new_api_key
      revealDialog.action = result.action
      revealDialog.open = true
    } else {
      notify('API Key 已吊销')
    }
    if (result.session_ended) {
      window.location.assign('/login')
      return
    }
    await load()
    if (userSession?.value && result.new_key_id && actionDialog.target?.id === userSession.value.login_key_id) {
      userSession.value.login_key_id = result.new_key_id
    }
  } catch (exc) {
    notify(exc.message, 'error')
  } finally {
    actionLoading.value = false
  }
}

function openRename(key) {
  renameDialog.key = key
  renameDialog.name = key.name || ''
  renameDialog.open = true
}

async function rename() {
  actionLoading.value = true
  try {
    await api(`/api/me/keys/${renameDialog.key.id}`, { method: 'PATCH', body: { name: renameDialog.name } })
    renameDialog.open = false
    notify('名称已更新')
    await load()
  } catch (exc) {
    notify(exc.message, 'error')
  } finally {
    actionLoading.value = false
  }
}

async function copyKey() {
  try {
    await navigator.clipboard.writeText(revealDialog.apiKey)
    notify('完整 API Key 已复制')
  } catch {
    notify('浏览器未允许复制，请手动选中 Key', 'warning')
  }
}

onMounted(load)
</script>

<template>
  <div class="content-shell">
    <PageHeader title="我的 API Key" subtitle="新增、命名、重置与吊销">
      <template #actions>
        <v-btn color="primary" @click="openAction('add')"><Plus :size="17" class="mr-2" />新增 Key</v-btn>
        <v-tooltip text="刷新 Key 列表"><template #activator="{ props }"><v-btn v-bind="props" icon variant="outlined" :loading="loading" @click="load"><RefreshCw :size="18" /></v-btn></template></v-tooltip>
      </template>
    </PageHeader>

    <section class="section-band">
      <div class="section-band__head"><div><h2>API Keys</h2><p>完整 Key 仅在创建或重置成功后显示一次</p></div></div>
      <div class="section-band__body section-band__body--flush">
        <LoadingState :loading="loading" :error="error" :empty="!data?.keys?.length" empty-text="暂无 API Key" @retry="load">
          <v-data-table :headers="headers" :items="data?.keys || []" :items-per-page="25">
            <template #item.masked="{ item }">
              <div class="d-flex align-center ga-2"><KeyRound :size="16" class="data-muted" /><span class="mono">{{ item.masked }}</span></div>
            </template>
            <template #item.name="{ item }"><span>{{ item.name || '-' }}</span></template>
            <template #item.status="{ item }"><v-chip :color="item.status === 'active' ? 'success' : 'default'" variant="tonal">{{ item.status }}</v-chip></template>
            <template #item.created_at="{ item }">{{ dateTime(item.created_at) }}</template>
            <template #item.revoked_at="{ item }">{{ dateTime(item.revoked_at) }}</template>
            <template #item.actions="{ item }">
              <div class="d-flex justify-end ga-1">
                <v-tooltip text="修改名称"><template #activator="{ props }"><v-btn v-bind="props" icon size="small" variant="text" @click="openRename(item)"><Pencil :size="16" /></v-btn></template></v-tooltip>
                <template v-if="item.status === 'active'">
                  <v-tooltip text="重置 Key"><template #activator="{ props }"><v-btn v-bind="props" icon size="small" variant="text" color="secondary" @click="openAction('reset', item)"><RotateCw :size="16" /></v-btn></template></v-tooltip>
                  <v-tooltip text="吊销 Key"><template #activator="{ props }"><v-btn v-bind="props" icon size="small" variant="text" color="error" @click="openAction('revoke', item)"><Trash2 :size="16" /></v-btn></template></v-tooltip>
                </template>
              </div>
            </template>
          </v-data-table>
        </LoadingState>
      </div>
    </section>

    <v-dialog v-model="actionDialog.open" max-width="520" persistent>
      <v-card>
        <v-card-title>{{ dialogTitle }}</v-card-title>
        <v-card-text>
          <v-alert v-if="actionDialog.action === 'revoke'" type="error" variant="tonal" border="start" class="mb-4">
            吊销后该 Key 的所有 Web 会话立即失效。
          </v-alert>
          <v-text-field v-model="actionDialog.currentKey" label="重新输入任一当前有效 API Key" type="password" autocomplete="current-password" autofocus />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" :disabled="actionLoading" @click="actionDialog.open = false">取消</v-btn>
          <v-btn :color="actionDialog.action === 'revoke' ? 'error' : 'primary'" :loading="actionLoading" :disabled="!actionDialog.currentKey" @click="executeAction">
            {{ actionDialog.action === 'add' ? '新增' : actionDialog.action === 'reset' ? '重置' : '吊销' }}
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="renameDialog.open" max-width="480">
      <v-card>
        <v-card-title>修改 Key 名称</v-card-title>
        <v-card-text><v-text-field v-model="renameDialog.name" label="名称" maxlength="120" autofocus @keyup.enter="rename" /></v-card-text>
        <v-card-actions><v-spacer /><v-btn variant="text" @click="renameDialog.open = false">取消</v-btn><v-btn color="primary" :loading="actionLoading" @click="rename">保存</v-btn></v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="revealDialog.open" max-width="620" persistent>
      <v-card>
        <v-card-title>{{ revealDialog.action === 'reset' ? 'Key 已重置' : 'Key 已创建' }}</v-card-title>
        <v-card-text>
          <v-alert type="warning" variant="tonal" border="start" class="mb-4">关闭后将无法再次查看完整 Key。</v-alert>
          <div class="key-reveal mono">{{ revealDialog.apiKey }}</div>
        </v-card-text>
        <v-card-actions>
          <v-btn variant="outlined" @click="copyKey"><Copy :size="17" class="mr-2" />复制</v-btn>
          <v-spacer />
          <v-btn color="primary" @click="revealDialog.open = false; revealDialog.apiKey = ''">我已保存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="4500">{{ snackbar.text }}</v-snackbar>
  </div>
</template>
