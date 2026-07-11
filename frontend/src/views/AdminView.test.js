// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import vuetify from '../plugins/vuetify'
import AdminView from './AdminView.vue'

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

const snapshot = {
  reconciliation: { ok: true },
  usage: { recent_cost: '0.0000' },
  accounts: { accounts: [] },
  admin: {
    cycles: [{
      id: 1,
      name: 'cycle-open',
      status: 'open',
      pool_costs: [{ pool_id: 1, pool: 'default-cpa', fixed_cost: '10.00' }],
    }],
    users: [{ id: 2, name: '@u2', registered: true, manual_allowed: false, active_keys: 1 }],
    pools: [{ id: 1, name: 'default-cpa', active: true, rules: [] }],
    gradients: [],
    keys: [],
    ownership: [],
    pricing: [],
    adjustments: [],
    manual_usage_adjustments: [],
    sync: [],
    dead_letters: [],
    audits: [],
  },
}

describe('AdminView manual usage', () => {
  beforeEach(() => {
    api.mockImplementation((url) => {
      if (url === '/api/admin/snapshot') return Promise.resolve(snapshot)
      if (url === '/api/admin/manual-usage-adjustments') return Promise.resolve({ ok: true, id: 1 })
      return Promise.resolve({ ok: true })
    })
  })

  afterEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
  })

  it('submits raw equivalent usage and refreshes the admin snapshot', async () => {
    const wrapper = mount(AdminView, {
      attachTo: document.body,
      global: { plugins: [vuetify] },
    })
    await flushPromises()

    const adjustmentsTab = wrapper.findAllComponents({ name: 'VTab' })
      .find((item) => item.text().includes('调整与归属'))
    expect(adjustmentsTab).toBeTruthy()
    await adjustmentsTab.trigger('click')
    await flushPromises()

    const openButton = wrapper.findAllComponents({ name: 'VBtn' })
      .find((item) => item.text().includes('添加原始用量'))
    expect(openButton).toBeTruthy()
    await openButton.trigger('click')
    await flushPromises()

    const dialog = wrapper.findAllComponents({ name: 'VDialog' })
      .find((item) => item.props('modelValue') === true)
    expect(dialog).toBeTruthy()
    const amount = dialog.findAllComponents({ name: 'VTextField' })
      .find((item) => item.props('label') === '原始等效用量（USD）')
    const reason = dialog.findAllComponents({ name: 'VTextarea' })
      .find((item) => item.props('label') === '原因')
    await amount.setValue('2.500000001')
    await reason.setValue('人工补录测试')

    const submit = dialog.findAllComponents({ name: 'VBtn' })
      .find((item) => item.text().includes('计入账期'))
    await submit.trigger('click')
    await flushPromises()

    expect(api).toHaveBeenCalledWith('/api/admin/manual-usage-adjustments', {
      admin: true,
      body: {
        cycle: 'cycle-open',
        pool_id: 1,
        telegram_user_id: 2,
        amount_usd: '2.500000001',
        reason: '人工补录测试',
      },
      method: 'POST',
    })
    expect(api.mock.calls.filter(([url]) => url === '/api/admin/snapshot')).toHaveLength(2)
    wrapper.unmount()
  })
})
