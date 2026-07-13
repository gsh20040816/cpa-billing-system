// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import vuetify from '../plugins/vuetify'
import AccountsView from './AccountsView.vue'

vi.mock('../api', () => ({ api: vi.fn() }))

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = ResizeObserverStub
globalThis.visualViewport = {
  width: 1280,
  height: 800,
  offsetLeft: 0,
  offsetTop: 0,
  addEventListener() {},
  removeEventListener() {},
}

describe('AccountsView quota estimates', () => {
  beforeEach(() => {
    api.mockResolvedValue({
      inspection: { total: 1, normal: 1, limit_reached: 0, unauthorized_401_402: 0, other_failed: 0, cached: 1 },
      accounts: [{
        id: 'account-1',
        name: 'Shared Pro',
        type: 'codex',
        plan_type: 'pro',
        auth_type: 'oauth',
        quota_status: 'completed',
        can_refresh: true,
        disabled: false,
        usage: { requests: 2, total_tokens: 2000, success_rate: 100, cost: '100.0000', last_used_at: null, unpriced: 0 },
        quota: [{
          key: 'weekly',
          label: '周额度',
          used_percent: 42,
          reset_at: '2026-07-20T03:02:00+08:00',
          window_usage_requests: 2,
          window_usage_tokens: 2000,
          window_usage_cost: '100.0000',
          window_unpriced: 0,
          usage_filter: { mode: 'all_models', models: [], display_models: [] },
          available_estimate: {
            status: 'estimated',
            estimated_total_cost_lower: '235.2941',
            estimated_total_cost_upper: '240.9639',
            available_cost_lower: '135.2941',
            available_cost_upper: '140.9639',
            available_percent_min: '57.5',
            available_percent_max: '58.5',
          },
        }],
        quota_refreshed_at: '2026-07-14T12:00:00+08:00',
        reset_credits_available: 0,
        reset_credits: [],
        reset_credits_error: null,
      }],
    })
  })

  afterEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
  })

  it('labels total quota and remaining quota separately', async () => {
    const wrapper = mount(AccountsView, {
      attachTo: document.body,
      global: { plugins: [vuetify] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('本周期预估总额度')
    expect(wrapper.text()).toContain('$235.2941 – $240.9639')
    expect(wrapper.text()).toContain('本周期剩余额度')
    expect(wrapper.text()).toContain('$135.2941 – $140.9639')
    expect(wrapper.text()).toContain('剩余比例 57.5%–58.5%')
    expect(wrapper.text()).not.toContain('本周期预计可用额度')
    wrapper.unmount()
  })
})
