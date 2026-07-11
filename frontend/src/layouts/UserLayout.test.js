// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import vuetify from '../plugins/vuetify'
import UserLayout from './UserLayout.vue'

vi.mock('../api', () => ({
  api: vi.fn(),
  clearCsrf: vi.fn(),
  loadUserSession: vi.fn().mockResolvedValue({
    telegram_user_id: 100,
    name: '@tester',
    is_admin: false,
  }),
}))

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = ResizeObserverStub

describe('UserLayout', () => {
  it('opens the permanent navigation drawer on desktop', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 })
    const View = { template: '<div />' }
    const router = createRouter({
      history: createMemoryHistory(),
      routes: ['/', '/requests', '/status', '/accounts', '/rankings', '/pricing', '/keys', '/admin/login']
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
})
