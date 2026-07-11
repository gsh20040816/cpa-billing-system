// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import vuetify from '../plugins/vuetify'
import RankingsView from './RankingsView.vue'

vi.mock('../api', () => ({ api: vi.fn() }))
vi.mock('../charts', () => ({ VChart: { name: 'VChart', props: ['option'], template: '<div class="chart-stub" />' } }))

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = ResizeObserverStub

describe('RankingsView', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    api.mockResolvedValue({
      range: { start: '2026-07-10', end: '2026-07-11' },
      rows: [],
      totals: { requests: 0, tokens: 0, cost: '0', failed: 0 },
    })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  it('reloads automatically when the range changes', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: RankingsView }],
    })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(RankingsView, { global: { plugins: [router, vuetify] } })
    await flushPromises()

    expect(api).toHaveBeenCalledOnce()
    expect(api.mock.calls[0][0]).toContain('sort=cost')
    expect(wrapper.text()).not.toContain('应用')
    const sevenDays = wrapper.findAll('button').find((button) => button.text().trim() === '7d')
    expect(sevenDays).toBeTruthy()
    await sevenDays.trigger('click')
    await vi.advanceTimersByTimeAsync(180)
    await flushPromises()

    expect(api).toHaveBeenCalledTimes(2)
    expect(api.mock.calls[1][0]).toContain('range=7d')
    wrapper.unmount()
  })

  it('renders the default cost chart in USD instead of nano-USD', async () => {
    api.mockResolvedValue({
      range: { start: '2026-07-10', end: '2026-07-11' },
      rows: [
        {
          name: '@GSHgsh0', telegram_user_id: 1, requests: 1903, tokens: 354480163,
          cost: '326.4468', cost_nano_usd: 326446827700, failed: 1, success_rate: 99.95,
          long_context: 360, key_count: 1,
        },
      ],
      totals: { requests: 1903, tokens: 354480163, cost: '326.4468', failed: 1 },
    })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: RankingsView }],
    })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(RankingsView, { global: { plugins: [router, vuetify] } })
    await flushPromises()

    const option = wrapper.findComponent({ name: 'VChart' }).props('option')
    expect(option.xAxis.name).toBe('USD')
    expect(option.series[0].data).toEqual([326.4468])
    expect(option.tooltip.valueFormatter(326.4468)).toBe('$326.4468')
    expect(option.xAxis.axisLabel.formatter(326.4468)).toBe('$326.4468')
    wrapper.unmount()
  })
})
