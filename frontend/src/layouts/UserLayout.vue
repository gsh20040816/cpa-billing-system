<script setup>
import { computed, onMounted, provide, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDisplay } from 'vuetify'
import {
  Activity, BarChart3, BookOpenText, ChevronDown, CircleDollarSign,
  Gauge, KeyRound, LayoutDashboard, LogOut, Menu, ServerCog, Settings2,
} from '@lucide/vue'
import { api, clearCsrf, loadUserSession } from '../api'
import SystemPulse from '../components/SystemPulse.vue'

const route = useRoute()
const router = useRouter()
const { mdAndUp } = useDisplay()
const drawer = ref(false)
const loading = ref(true)
const session = ref(null)

const nav = [
  { title: '账务总览', to: '/', icon: LayoutDashboard },
  { title: '历史请求', to: '/requests', icon: Activity },
  { title: '全站状态', to: '/status', icon: Gauge },
  { title: '上游账号', to: '/accounts', icon: ServerCog },
  { title: '用量排行', to: '/rankings', icon: BarChart3 },
  { title: '费用规则', to: '/pricing', icon: CircleDollarSign },
  { title: '我的 API Key', to: '/keys', icon: KeyRound },
]
const managementNav = [
  { title: '系统管理', to: '/admin', icon: Settings2 },
  { title: '全部请求', to: '/admin/requests', icon: Activity },
]
const readOnlyNav = nav.filter((item) => ['/requests', '/status', '/accounts'].includes(item.to))
const readOnlyPaths = new Set(readOnlyNav.map((item) => item.to))

const readOnlyGuest = computed(() => Boolean(session.value?.read_only))
const userNav = computed(() => readOnlyGuest.value ? readOnlyNav : nav)
const bottomNav = computed(() => [
  ...userNav.value.filter((item) => ['/', '/requests', '/status', '/keys'].includes(item.to)),
  ...(session.value?.is_admin ? [managementNav[0]] : []),
])
const pageTitle = computed(() => route.meta.title || 'CPA Billing')

provide('userSession', session)

watch(mdAndUp, (desktop) => {
  drawer.value = desktop
}, { immediate: true })

async function loadSession() {
  try {
    session.value = await loadUserSession()
    if (readOnlyGuest.value && !readOnlyPaths.has(route.path)) {
      await router.replace('/requests')
    }
  } catch (exc) {
    if (exc?.status !== 401) window.location.assign('/login')
  } finally {
    loading.value = false
  }
}

watch(() => route.path, (path) => {
  if (readOnlyGuest.value && !readOnlyPaths.has(path)) router.replace('/requests')
})

async function logout() {
  try {
    await api('/auth/logout', { method: 'POST' })
  } finally {
    clearCsrf()
    clearCsrf(true)
    window.location.assign('/login')
  }
}

onMounted(loadSession)
</script>

<template>
  <v-app v-if="!loading" class="app-main">
    <v-navigation-drawer v-model="drawer" :permanent="mdAndUp" :temporary="!mdAndUp" width="252">
      <div class="brand-block">
        <div class="brand-mark">CPA</div>
        <div>
          <div class="brand-name">Billing Console</div>
          <div class="brand-caption">usage · quota · allocation</div>
        </div>
      </div>
      <v-divider />
      <v-list nav density="compact" class="pa-2">
        <v-list-item v-for="item in userNav" :key="item.to" :to="item.to" :exact="item.to === '/'" color="primary" rounded="sm">
          <template #prepend><component :is="item.icon" :size="19" /></template>
          <v-list-item-title>{{ item.title }}</v-list-item-title>
        </v-list-item>
        <template v-if="session?.is_admin">
          <v-list-subheader>管理</v-list-subheader>
          <v-list-item v-for="item in managementNav" :key="item.to" :to="item.to" :exact="item.to === '/admin'" color="primary" rounded="sm">
            <template #prepend><component :is="item.icon" :size="19" /></template>
            <v-list-item-title>{{ item.title }}</v-list-item-title>
          </v-list-item>
        </template>
      </v-list>
      <template #append>
        <v-divider />
        <v-list density="compact" class="pa-2">
          <v-list-item href="https://cpa.shgao.top" target="_blank" title="CPA API">
            <template #prepend><BookOpenText :size="18" /></template>
          </v-list-item>
        </v-list>
      </template>
    </v-navigation-drawer>

    <v-app-bar flat color="surface" border="b" height="58">
      <v-btn v-if="!mdAndUp" icon variant="text" @click="drawer = true"><Menu :size="21" /></v-btn>
      <v-app-bar-title class="app-bar-title">{{ pageTitle }}</v-app-bar-title>
      <v-menu>
        <template #activator="{ props }">
          <v-btn v-bind="props" variant="text" class="user-menu-btn">
            <span class="user-menu-name">{{ session?.name }}</span>
            <ChevronDown :size="16" />
          </v-btn>
        </template>
        <v-list density="compact" min-width="190">
          <v-list-item
            :title="session?.management_session ? '管理 Token 会话' : session?.read_only ? '未绑定 API Key（只读）' : 'Telegram 用户'"
            :subtitle="session?.telegram_user_id ? String(session.telegram_user_id) : session?.read_only ? '仅历史请求、全站状态、上游账号' : '全站管理权限'"
          />
          <v-divider />
          <v-list-item title="退出登录" @click="logout">
            <template #prepend><LogOut :size="17" /></template>
          </v-list-item>
        </v-list>
      </v-menu>
    </v-app-bar>

    <v-main>
      <SystemPulse />
      <router-view />
    </v-main>

    <v-bottom-navigation v-if="!mdAndUp" grow color="primary" height="64">
      <v-btn v-for="item in bottomNav" :key="item.to" :to="item.to" :exact="item.to === '/'" stacked>
        <component :is="item.icon" :size="20" />
        <span>{{ item.title.replace('API Key', 'Key') }}</span>
      </v-btn>
    </v-bottom-navigation>
  </v-app>
</template>

<style scoped>
.brand-block { min-height: 76px; display: flex; align-items: center; gap: 12px; padding: 14px 16px; }
.brand-mark { width: 42px; height: 42px; display: grid; place-items: center; background: #202427; color: #f4f7f6; border-radius: 5px; font-family: 'JetBrains Mono', monospace; font-weight: 700; }
.brand-name { font-weight: 720; line-height: 1.1; }
.brand-caption { color: #78817f; font-family: 'JetBrains Mono', monospace; font-size: 0.64rem; margin-top: 4px; }
.app-bar-title { font-size: 0.98rem; font-weight: 680; }
.user-menu-btn { text-transform: none; }
.user-menu-name { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 600px) { .user-menu-name { max-width: 120px; } }
</style>
