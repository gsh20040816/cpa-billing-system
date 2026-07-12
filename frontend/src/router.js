import { createRouter, createWebHistory } from 'vue-router'

const UserLayout = () => import('./layouts/UserLayout.vue')
const LoginView = () => import('./views/LoginView.vue')
const DashboardView = () => import('./views/DashboardView.vue')
const RequestsView = () => import('./views/RequestsView.vue')
const SiteStatusView = () => import('./views/SiteStatusView.vue')
const AccountsView = () => import('./views/AccountsView.vue')
const RankingsView = () => import('./views/RankingsView.vue')
const PricingView = () => import('./views/PricingView.vue')
const KeysView = () => import('./views/KeysView.vue')
const UserView = () => import('./views/UserView.vue')
const AdminView = () => import('./views/AdminView.vue')

const routes = [
  { path: '/login', component: LoginView, meta: { public: true, title: '用户登录' } },
  { path: '/admin/login', redirect: '/login', meta: { public: true, title: '用户登录' } },
  {
    path: '/',
    component: UserLayout,
    children: [
      { path: '', component: DashboardView, meta: { title: '账务总览' } },
      { path: 'requests', component: RequestsView, meta: { title: '历史请求' } },
      { path: 'status', component: SiteStatusView, meta: { title: '全站状态' } },
      { path: 'accounts', component: AccountsView, meta: { title: '上游账号' } },
      { path: 'rankings', component: RankingsView, meta: { title: '用量排行' } },
      { path: 'pricing', component: PricingView, meta: { title: '费用规则' } },
      { path: 'keys', component: KeysView, meta: { title: '我的 API Key' } },
      { path: 'users/:id', component: UserView, meta: { title: '用户用量' } },
      { path: 'admin', component: AdminView, meta: { admin: true, title: '系统管理' } },
      { path: 'admin/requests', component: RequestsView, props: { admin: true }, meta: { admin: true, title: '全部请求' } },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.afterEach((to) => {
  document.title = `${to.meta.title || 'CPA Billing'} · CPA Billing`
})

export default router
