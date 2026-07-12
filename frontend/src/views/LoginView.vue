<script setup>
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Eye, EyeOff, KeyRound, LogIn, ShieldCheck } from '@lucide/vue'
import { api, setCsrf } from '../api'

const router = useRouter()
const mode = ref('api-key')
const apiKey = ref('')
const managementToken = ref('')
const visible = ref(false)
const loading = ref(false)
const error = ref('')
const credential = computed(() => mode.value === 'api-key' ? apiKey.value : managementToken.value)
const title = computed(() => mode.value === 'api-key' ? '用户登录' : '管理员登录')

async function submit() {
  if (!credential.value.trim()) return
  loading.value = true
  error.value = ''
  try {
    const result = mode.value === 'api-key'
      ? await api('/auth/api-key/login', { body: { api_key: apiKey.value } })
      : await api('/auth/admin/login', { body: { management_token: managementToken.value } })
    setCsrf(result.csrf_token)
    setCsrf(result.csrf_token, true)
    await router.replace('/')
  } catch (exc) {
    error.value = exc.message
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <v-app>
    <main class="login-surface">
      <section class="login-tool" aria-labelledby="login-title">
        <div class="login-tool__bar">
          <span class="login-tool__brand">CPA Billing</span>
          <span class="login-tool__mode">UNIFIED SESSION</span>
        </div>
        <div class="login-tool__body">
          <div class="d-flex align-center ga-3 mb-4">
            <v-avatar :color="mode === 'api-key' ? 'primary' : 'secondary'" rounded="sm">
              <KeyRound v-if="mode === 'api-key'" :size="22" /><ShieldCheck v-else :size="22" />
            </v-avatar>
            <div>
              <h1 id="login-title">{{ title }}</h1>
              <div class="data-muted text-body-2">CPA Billing</div>
            </div>
          </div>
          <v-btn-toggle v-model="mode" mandatory divided color="primary" class="login-mode-toggle mb-4">
            <v-btn value="api-key"><KeyRound :size="16" class="mr-2" />API Key</v-btn>
            <v-btn value="management"><ShieldCheck :size="16" class="mr-2" />管理 Token</v-btn>
          </v-btn-toggle>
          <v-alert v-if="error" type="error" variant="tonal" border="start" class="mb-4">{{ error }}</v-alert>
          <v-form @submit.prevent="submit">
            <v-text-field
              v-if="mode === 'api-key'"
              v-model="apiKey"
              label="API Key"
              :type="visible ? 'text' : 'password'"
              autocomplete="current-password"
              autofocus
            >
              <template #append-inner>
                <v-btn icon size="x-small" variant="text" @click="visible = !visible">
                  <EyeOff v-if="visible" :size="17" /><Eye v-else :size="17" />
                </v-btn>
              </template>
            </v-text-field>
            <v-text-field
              v-else
              v-model="managementToken"
              label="管理 Token"
              :type="visible ? 'text' : 'password'"
              autocomplete="current-password"
              autofocus
            >
              <template #append-inner>
                <v-btn icon size="x-small" variant="text" @click="visible = !visible">
                  <EyeOff v-if="visible" :size="17" /><Eye v-else :size="17" />
                </v-btn>
              </template>
            </v-text-field>
            <v-btn type="submit" color="primary" block size="large" :loading="loading" class="mt-4">
              <LogIn :size="18" class="mr-2" />登录
            </v-btn>
          </v-form>
          <v-divider class="my-5" />
          <div class="text-body-2 data-muted">首次注册仅通过 Telegram Bot 私聊执行 <span class="mono">/register</span>。登录后根据权限显示可用页面。</div>
        </div>
      </section>
    </main>
  </v-app>
</template>

<style scoped>
.login-mode-toggle { width: 100%; }
.login-mode-toggle :deep(.v-btn) { flex: 1; text-transform: none; }
</style>
