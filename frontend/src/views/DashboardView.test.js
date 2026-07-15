// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import vuetify from '../plugins/vuetify'
import DashboardView from './DashboardView.vue'

vi.mock('../api', () => ({ api: vi.fn() }))
vi.mock('../charts', () => ({ VChart: { name: 'VChart', props: ['option'], template: '<div class="chart-stub" />' } }))

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = ResizeObserverStub

describe('DashboardView rates', () => {
  beforeEach(() => {
    api.mockResolvedValue({
      cycle: { name: 'cycle', start: '2026-07-01 00:00', end: '2026-08-01 00:00', status: 'open' },
      cycles: [{ name: 'cycle', status: 'open' }],
      rows: [{
        telegram_user_id: 2, name: '@u2', requests: 1, tokens: 1000, actual: '1.0000',
        manual_actual: '0.0000', billed: '1.0000', amount: '3.00', user_rate: '3.000000',
        key_count: 1, unowned: false,
      }, {
        telegram_user_id: null, name: '未绑定 Telegram 的 API Key', requests: 1, tokens: 1000,
        actual: '1.0000', manual_actual: '0.0000', billed: '0.0000', amount: '7.00',
        user_rate: null, key_count: 1, unowned: true,
      }],
      metered_keys: [],
      pool_totals: [],
      models: [],
      totals: {
        requests: 2, tokens: 2000, actual: '2.0000', billed: '1.0000',
        fixed_cost: '10.00', metered_amount: '7.00', member_amount: '3.00', amount: '10.00',
        global_rate: '3.000000',
      },
    })
  })

  afterEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
  })

  it('shows the global rate and bound-user rates while leaving unbound keys unrated', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: DashboardView }],
    })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(DashboardView, {
      attachTo: document.body,
      global: { plugins: [vuetify, router] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('全局费率')
    expect(wrapper.text()).toContain('成员分摊 / 梯度计费用量')
    expect(wrapper.text()).toContain('¥3.000000/$')
    expect(wrapper.text()).toContain('用户费率 (¥/$)')
    expect(wrapper.text()).toContain('@u2')
    expect(wrapper.text()).toContain('未绑定 Telegram 的 API Key')
    expect(wrapper.text()).toContain('用户费率')
    wrapper.unmount()
  })
})
