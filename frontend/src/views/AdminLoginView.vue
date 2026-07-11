<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowLeft, Eye, EyeOff, LogIn, ShieldCheck } from '@lucide/vue'
import { api, setCsrf } from '../api'

const router = useRouter()
const token = ref('')
const visible = ref(false)
const loading = ref(false)
const error = ref('')

async function submit() {
  if (!token.value.trim()) return
  loading.value = true
  error.value = ''
  try {
    const result = await api('/auth/admin/login', { admin: true, body: { management_token: token.value } })
    setCsrf(result.csrf_token, true)
    await router.replace('/admin')
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
      <section class="login-tool" aria-labelledby="admin-login-title">
        <div class="login-tool__bar">
          <span class="login-tool__brand">CPA Billing</span>
          <span class="login-tool__mode">ADMIN SESSION</span>
        </div>
        <div class="login-tool__body">
          <div class="d-flex align-center ga-3 mb-4">
            <v-avatar color="secondary" rounded="sm"><ShieldCheck :size="22" /></v-avatar>
            <div>
              <h1 id="admin-login-title">系统管理</h1>
              <div class="data-muted text-body-2">独立管理凭据</div>
            </div>
          </div>
          <v-alert v-if="error" type="error" variant="tonal" border="start" class="mb-4">{{ error }}</v-alert>
          <v-form @submit.prevent="submit">
            <v-text-field
              v-model="token"
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
            <v-btn type="submit" color="secondary" block size="large" :loading="loading" class="mt-4">
              <LogIn :size="18" class="mr-2" />进入管理面板
            </v-btn>
          </v-form>
          <v-btn to="/login" variant="text" color="primary" class="mt-4 px-0">
            <ArrowLeft :size="17" class="mr-2" />返回用户登录
          </v-btn>
        </div>
      </section>
    </main>
  </v-app>
</template>
