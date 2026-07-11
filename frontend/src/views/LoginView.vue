<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { Eye, EyeOff, KeyRound, LogIn, ShieldCheck } from '@lucide/vue'
import { api, setCsrf } from '../api'

const router = useRouter()
const apiKey = ref('')
const visible = ref(false)
const loading = ref(false)
const error = ref('')

async function submit() {
  if (!apiKey.value.trim()) return
  loading.value = true
  error.value = ''
  try {
    const result = await api('/auth/api-key/login', { body: { api_key: apiKey.value } })
    setCsrf(result.csrf_token)
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
          <span class="login-tool__mode">USER SESSION</span>
        </div>
        <div class="login-tool__body">
          <div class="d-flex align-center ga-3 mb-4">
            <v-avatar color="primary" rounded="sm"><KeyRound :size="22" /></v-avatar>
            <div>
              <h1 id="login-title">用户登录</h1>
              <div class="data-muted text-body-2">Telegram 注册用户</div>
            </div>
          </div>
          <v-alert v-if="error" type="error" variant="tonal" border="start" class="mb-4">{{ error }}</v-alert>
          <v-form @submit.prevent="submit">
            <v-text-field
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
            <v-btn type="submit" color="primary" block size="large" :loading="loading" class="mt-4">
              <LogIn :size="18" class="mr-2" />登录
            </v-btn>
          </v-form>
          <v-divider class="my-5" />
          <div class="text-body-2 data-muted">首次注册仅通过 Telegram Bot 私聊执行 <span class="mono">/register</span></div>
          <v-btn to="/admin/login" variant="text" color="secondary" class="mt-3 px-0">
            <ShieldCheck :size="17" class="mr-2" />管理入口
          </v-btn>
        </div>
      </section>
    </main>
  </v-app>
</template>
