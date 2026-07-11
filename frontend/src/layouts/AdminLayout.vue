<script setup>
import { onMounted, provide, ref } from 'vue'
import { LogOut, ShieldCheck } from '@lucide/vue'
import { api, clearCsrf, loadAdminSession } from '../api'

const loading = ref(true)
const session = ref(null)
provide('adminSession', session)

async function loadSession() {
  try {
    session.value = await loadAdminSession()
  } catch {
    window.location.assign('/admin/login')
  } finally {
    loading.value = false
  }
}

async function logout() {
  try {
    await api('/auth/admin/logout', { method: 'POST', admin: true })
  } finally {
    clearCsrf(true)
    window.location.assign('/admin/login')
  }
}

onMounted(loadSession)
</script>

<template>
  <v-app v-if="!loading">
    <v-app-bar color="#202427" flat height="60">
      <div class="admin-brand"><ShieldCheck :size="21" /><span>CPA Billing 管理</span></div>
      <v-spacer />
      <v-btn color="white" variant="text" @click="logout"><LogOut :size="17" class="mr-2" />退出管理</v-btn>
    </v-app-bar>
    <v-main>
      <router-view />
    </v-main>
  </v-app>
</template>

<style scoped>
.admin-brand { display: flex; align-items: center; gap: 10px; padding-left: 22px; color: #f4f7f6; font-weight: 700; }
</style>
