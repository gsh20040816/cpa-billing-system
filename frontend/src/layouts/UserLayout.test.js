// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import vuetify from '../plugins/vuetify'
import UserLayout from './UserLayout.vue'
import { loadUserSession } from '../api'

vi.mock('../api', () => ({
  api: vi.fn(),
  clearCsrf: vi.fn(),
  loadUserSession: vi.fn(),
}))

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = ResizeObserverStub

describe('UserLayout', () => {
  beforeEach(() => {
    loadUserSession.mockResolvedValue({
      telegram_user_id: 100,
      name: '@tester',
      is_admin: false,
      management_session: false,
    })
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('opens the permanent navigation drawer on desktop', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 })
    const View = { template: '<div />' }
    const router = createRouter({
      history: createMemoryHistory(),
      routes: ['/', '/requests', '/status', '/accounts', '/rankings', '/pricing', '/keys', '/admin', '/admin/requests']
        .map((path) => ({ path, component: View })),
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(UserLayout, {
      global: {
        plugins: [router, vuetify],
        stubs: { SystemPulse: true, RouterView: true },
      },
    })
    await flushPromises()

    const drawer = wrapper.findComponent({ name: 'VNavigationDrawer' })
    expect(drawer.exists()).toBe(true)
    expect(drawer.props('modelValue')).toBe(true)
    expect(drawer.props('permanent')).toBe(true)
    expect(wrapper.find('.v-navigation-drawer--active').exists()).toBe(true)
  })

  it('uses exact matching for the dashboard navigation item', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 })
    const View = { template: '<div />' }
    const router = createRouter({
      history: createMemoryHistory(),
      routes: ['/', '/requests', '/status', '/accounts', '/rankings', '/pricing', '/keys', '/admin', '/admin/requests']
        .map((path) => ({ path, component: View })),
    })
    await router.push('/status')
    await router.isReady()

    const wrapper = mount(UserLayout, {
      global: {
        plugins: [router, vuetify],
        stubs: { SystemPulse: true, RouterView: true },
      },
    })
    await flushPromises()

    const dashboard = wrapper.findAllComponents({ name: 'VListItem' })
      .find((item) => item.props('to') === '/')
    expect(dashboard.props('exact')).toBe(true)
    expect(dashboard.classes()).not.toContain('v-list-item--active')
  })

  it('adds the management navigation for a Telegram web administrator', async () => {
    loadUserSession.mockResolvedValue({
      telegram_user_id: 100,
      name: '@tester',
      is_admin: true,
      management_session: false,
    })
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 })
    const View = { template: '<div />' }
    const router = createRouter({
      history: createMemoryHistory(),
      routes: ['/', '/requests', '/status', '/accounts', '/rankings', '/pricing', '/keys', '/admin', '/admin/requests']
        .map((path) => ({ path, component: View })),
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(UserLayout, {
      global: {
        plugins: [router, vuetify],
        stubs: { SystemPulse: true, RouterView: true },
      },
    })
    await flushPromises()

    const management = wrapper.findAllComponents({ name: 'VListItem' })
    expect(management.some((item) => item.props('to') === '/admin')).toBe(true)
    expect(management.some((item) => item.props('to') === '/admin/requests')).toBe(true)
    wrapper.unmount()
  })

  it('redirects an unowned-key session to the three read-only pages', async () => {
    loadUserSession.mockResolvedValue({
      telegram_user_id: null,
      name: '未绑定 API Key',
      is_admin: false,
      management_session: false,
      read_only: true,
    })
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 })
    const View = { template: '<div />' }
    const router = createRouter({
      history: createMemoryHistory(),
      routes: ['/', '/requests', '/status', '/accounts', '/rankings', '/pricing', '/keys']
        .map((path) => ({ path, component: View })),
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(UserLayout, {
      global: {
        plugins: [router, vuetify],
        stubs: { SystemPulse: true, RouterView: true },
      },
    })
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/requests')
    const destinations = wrapper.findAllComponents({ name: 'VListItem' })
      .map((item) => item.props('to'))
      .filter(Boolean)
    expect(destinations).toEqual(['/requests', '/status', '/accounts'])
    wrapper.unmount()
  })
})
