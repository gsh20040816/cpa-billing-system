<script setup>
import { onMounted, provide, ref } from 'vue'
import { Activity, LogOut, Settings2, ShieldCheck } from '@lucide/vue'
import { useDisplay } from 'vuetify'
import { api, clearCsrf, loadAdminSession } from '../api'

const { mdAndUp } = useDisplay()
const loading = ref(true)
const session = ref(null)
const nav = [
  { title: '系统管理', to: '/admin', icon: Settings2 },
  { title: '全部请求', to: '/admin/requests', icon: Activity },
]
provide('adminSession', session)

async function loadSession() {
  try {
    session.value = await loadAdminSession()
  } catch (exc) {
    if (exc?.status !== 401) window.location.assign('/admin/login')
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
      <v-tabs v-if="mdAndUp" color="#9fd8c5" height="60" class="admin-nav">
        <v-tab v-for="item in nav" :key="item.to" :to="item.to" :exact="item.to === '/admin'">
          <component :is="item.icon" :size="17" class="mr-2" />{{ item.title }}
        </v-tab>
      </v-tabs>
      <v-spacer />
      <v-btn color="white" variant="text" @click="logout"><LogOut :size="17" :class="mdAndUp ? 'mr-2' : ''" /><span v-if="mdAndUp">退出管理</span></v-btn>
    </v-app-bar>
    <v-main>
      <router-view />
    </v-main>
    <v-bottom-navigation v-if="!mdAndUp" grow color="primary" height="64">
      <v-btn v-for="item in nav" :key="item.to" :to="item.to" :exact="item.to === '/admin'" stacked>
        <component :is="item.icon" :size="20" />
        <span>{{ item.title }}</span>
      </v-btn>
    </v-bottom-navigation>
  </v-app>
</template>

<style scoped>
.admin-brand { display: flex; align-items: center; gap: 10px; padding-left: 22px; color: #f4f7f6; font-weight: 700; }
.admin-nav { margin-left: 24px; color: #d7dfdc; }
.admin-nav :deep(.v-tab) { min-width: 118px; letter-spacing: 0; text-transform: none; }
@media (max-width: 700px) { .admin-brand { padding-left: 16px; } }
</style>
