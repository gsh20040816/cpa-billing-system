// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import vuetify from '../plugins/vuetify'
import AdminLayout from './AdminLayout.vue'

vi.mock('../api', () => ({
  api: vi.fn(),
  clearCsrf: vi.fn(),
  loadAdminSession: vi.fn().mockResolvedValue({ is_admin: true }),
}))

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = ResizeObserverStub

describe('AdminLayout', () => {
  it('provides exact system and all-request navigation', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 })
    const View = { template: '<div />' }
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/admin', component: View },
        { path: '/admin/requests', component: View },
      ],
    })
    await router.push('/admin/requests')
    await router.isReady()

    const wrapper = mount(AdminLayout, {
      global: {
        plugins: [router, vuetify],
        stubs: { RouterView: true },
      },
    })
    await flushPromises()

    const tabs = wrapper.findAllComponents({ name: 'VTab' })
    const system = tabs.find((item) => item.props('to') === '/admin')
    const requests = tabs.find((item) => item.props('to') === '/admin/requests')
    expect(system).toBeTruthy()
    expect(system.props('exact')).toBe(true)
    expect(requests).toBeTruthy()
    expect(wrapper.text()).toContain('全部请求')
    wrapper.unmount()
  })
})
