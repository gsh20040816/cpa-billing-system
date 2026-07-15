// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import vuetify from '../plugins/vuetify'
import SiteStatusView from './SiteStatusView.vue'

vi.mock('../api', () => ({ api: vi.fn() }))
vi.mock('../charts', () => ({ VChart: { name: 'VChart', props: ['option'], template: '<div class="chart-stub" />' } }))

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

describe('SiteStatusView', () => {
  beforeEach(() => {
    api.mockResolvedValue({
      generated_at: '2026-07-15T13:00:00+08:00',
      range: {
        name: '60m',
        start: '2026-07-15T11:00:00+08:00',
        end: '2026-07-15T13:00:00+08:00',
      },
      cpa: { reachable: true, latency_ms: 12, api_key_count: 1 },
      accounts: { available: true, inspection: { total: 1, normal: 1, limit_reached: 0 } },
      billing: { sync: [{ source: 'cpamp', backlog: 0, last_error: null }] },
      overview: {
        summary: { request_count: 120, token_count: 120000, rpm: 60, tpm: 60000, total_cost: '1.2000', cost_available: true },
        service_health: { total_success: 120, total_failure: 0, success_rate: 100, block_details: [] },
      },
      realtime: {
        token_velocity: [{ bucket: '2026-07-15T12:00:00+08:00', tokens_per_minute: 1000 }],
        response_level: [{ bucket: '2026-07-15T12:00:00+08:00', ttft_p50_ms: 100, ttft_p95_ms: 200, latency_p50_ms: 500, latency_p95_ms: 800 }],
        cache_level: [{ bucket: '2026-07-15T12:00:00+08:00', cache_hit_rate: 50 }],
        token_efficiency: [{
          bucket: '2026-07-15T12:00:00+08:00',
          models: [
            { label: 'gpt-5.5', tokens_per_dollar: 1000000 },
            { label: 'gpt-5.6-sol', tokens_per_dollar: 2000000 },
          ],
        }],
        current_usage: { models: [], api_keys: { count: 1 }, upstream_accounts: [] },
      },
      degraded: false,
      errors: [],
    })
  })

  afterEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
  })

  it('renders model token efficiency as a line chart and shows the effective sample window', async () => {
    const wrapper = mount(SiteStatusView, {
      attachTo: document.body,
      global: { plugins: [vuetify] },
    })
    await flushPromises()

    expect(api).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('模型 Token 效率')

    const charts = wrapper.findAllComponents({ name: 'VChart' })
    expect(charts).toHaveLength(5)
    const option = charts[4].props('option')
    expect(option.series.map((item) => item.name)).toEqual(['gpt-5.5', 'gpt-5.6-sol'])
    expect(option.series[0].data).toEqual([1000000])
    expect(option.yAxis.name).toBe('Tokens / $1')
    wrapper.unmount()
  })
})
