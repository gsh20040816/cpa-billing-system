// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import vuetify from '../plugins/vuetify'
import RankingsView from './RankingsView.vue'

vi.mock('../api', () => ({ api: vi.fn() }))
vi.mock('../charts', () => ({ VChart: { template: '<div class="chart-stub" />' } }))

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
})
