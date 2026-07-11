// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import vuetify from '../plugins/vuetify'
import RequestsView from './RequestsView.vue'

vi.mock('../api', () => ({ api: vi.fn() }))

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = ResizeObserverStub
globalThis.visualViewport = {
  width: 1024,
  height: 768,
  offsetLeft: 0,
  offsetTop: 0,
  addEventListener() {},
  removeEventListener() {},
}

const event = {
  id: 1,
  request_id: 'acea13eb',
  occurred_at: '2026-07-11T22:05:04+08:00',
  model: 'gpt-5.6-sol',
  requested_model: 'gpt-5.6-sol',
  resolved_model: 'gpt-5.6-sol',
  service_tier: 'default',
  key: { id: 1, masked: 'sk-cpa-m..._vM4', name: null },
  tokens: { input: 66331, cache_read: 65024, cache_creation: 0, output: 799, reasoning: 516, total: 67130 },
  failed: false,
  status_code: null,
  latency_ms: 24350,
  ttft_ms: 949,
  generation_ms: 23401,
  tps: 34.15,
  cost: '0.0630',
  pricing_status: 'priced',
}

const VDataTableStub = {
  props: ['items'],
  emits: ['click:row'],
  template: '<button class="request-row" @click="$emit(\'click:row\', $event, { item: items[0] })">打开请求</button>',
}

const VDialogStub = {
  props: ['modelValue'],
  template: '<div v-if="modelValue"><slot /></div>',
}

describe('RequestsView', () => {
  beforeEach(() => {
    api.mockImplementation((url) => {
      if (url === '/api/me/usage/filter-options') {
        return Promise.resolve({ models: [], tiers: [], providers: [], failure_codes: [], keys: [] })
      }
      return Promise.resolve({
        items: [event],
        pagination: { total: 1, total_pages: 1, page: 1, page_size: 50 },
        summary: { requests: 1, input_tokens: 66331, output_tokens: 799, cost: '0.0630', failed: 0, unpriced: 0 },
      })
    })
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('shows one effective cache read field in request details', async () => {
    const wrapper = mount(RequestsView, {
      attachTo: document.body,
      global: {
        plugins: [vuetify],
        stubs: { VDataTable: VDataTableStub, VDialog: VDialogStub },
      },
    })
    await flushPromises()

    const row = wrapper.find('.request-row')
    expect(row.exists()).toBe(true)
    await row.trigger('click')
    await flushPromises()

    const pageText = document.body.textContent
    expect(pageText).toContain('缓存读取')
    expect(pageText).toContain('65,024')
    expect(pageText).toContain('缓存创建')
    expect(pageText).not.toContain('Cached')
    expect(pageText).not.toContain('Cache read')
    wrapper.unmount()
  })
})
