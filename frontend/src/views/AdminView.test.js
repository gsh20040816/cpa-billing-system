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
    cycles: [
      {
        id: 1,
        name: 'cycle-open',
        status: 'open',
        pool_costs: [{ pool_id: 1, pool: 'default-cpa', fixed_cost: '10.00' }],
      },
      {
        id: 2,
        name: 'cycle-target',
        status: 'open',
        pool_costs: [{ pool_id: 2, pool: 'target-pool', fixed_cost: '20.00' }],
      },
    ],
    users: [
      { id: 2, name: '@u2', registered: true, manual_allowed: false, active_keys: 1, is_admin: false, configured_admin: false },
      { id: 3, name: '@u3', registered: true, manual_allowed: false, active_keys: 1, is_admin: false, configured_admin: false },
    ],
    pools: [
      { id: 1, name: 'default-cpa', active: true, rules: [] },
      { id: 2, name: 'target-pool', active: true, rules: [] },
    ],
    gradients: [],
    keys: [],
    ownership: [],
    pricing: [],
    adjustments: [],
    manual_usage_adjustments: [{
      id: 7,
      cycle: 'cycle-open',
      pool_id: 1,
      pool: 'default-cpa',
      user_id: 2,
      user: '@u2',
      amount_usd: '1.25',
      reason: 'initial usage',
      created_at: '2026-07-11 20:00',
      updated_at: null,
      editable: true,
    }],
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

  it('edits every business field of an existing manual usage record', async () => {
    const wrapper = mount(AdminView, {
      attachTo: document.body,
      global: { plugins: [vuetify] },
    })
    await flushPromises()

    const adjustmentsTab = wrapper.findAllComponents({ name: 'VTab' })
      .find((item) => item.text().includes('调整与归属'))
    await adjustmentsTab.trigger('click')
    await flushPromises()
    const editButton = wrapper.findAllComponents({ name: 'VBtn' })
      .find((item) => item.text().includes('编辑'))
    expect(editButton).toBeTruthy()
    await editButton.trigger('click')
    await flushPromises()

    const dialog = wrapper.findAllComponents({ name: 'VDialog' })
      .find((item) => item.props('modelValue') === true)
    expect(dialog).toBeTruthy()
    const cycle = dialog.findAllComponents({ name: 'VSelect' })
      .find((item) => item.props('label') === '未关闭账期')
    const user = dialog.findAllComponents({ name: 'VAutocomplete' })
      .find((item) => item.props('label') === 'Telegram 用户')
    const amount = dialog.findAllComponents({ name: 'VTextField' })
      .find((item) => item.props('label') === '原始等效用量（USD）')
    const reason = dialog.findAllComponents({ name: 'VTextarea' })
      .find((item) => item.props('label') === '原因')
    expect(cycle.props('modelValue')).toBe('cycle-open')
    expect(user.props('modelValue')).toBe(2)
    expect(amount.props('modelValue')).toBe('1.25')

    await cycle.setValue('cycle-target')
    await user.setValue(3)
    await amount.setValue('3.500000001')
    await reason.setValue('updated usage')
    const submit = dialog.findAllComponents({ name: 'VBtn' })
      .find((item) => item.text().includes('保存修改'))
    await submit.trigger('click')
    await flushPromises()

    expect(api).toHaveBeenCalledWith('/api/admin/manual-usage-adjustments/7', {
      admin: true,
      body: {
        cycle: 'cycle-target',
        pool_id: 2,
        telegram_user_id: 3,
        amount_usd: '3.500000001',
        reason: 'updated usage',
      },
      method: 'PUT',
    })
    wrapper.unmount()
  })

  it('grants Web administration to a Telegram user', async () => {
    const wrapper = mount(AdminView, {
      attachTo: document.body,
      global: { plugins: [vuetify] },
    })
    await flushPromises()

    const identityTab = wrapper.findAllComponents({ name: 'VTab' })
      .find((item) => item.text().includes('用户与 Keys'))
    await identityTab.trigger('click')
    await flushPromises()

    const grantButton = wrapper.findAllComponents({ name: 'VBtn' })
      .find((item) => item.text().includes('授予管理'))
    expect(grantButton).toBeTruthy()
    await grantButton.trigger('click')
    await flushPromises()

    const dialog = wrapper.findAllComponents({ name: 'VDialog' })
      .find((item) => item.props('modelValue') === true)
    const reason = dialog.findComponent({ name: 'VTextarea' })
    await reason.setValue('授权管理测试')
    await dialog.findAllComponents({ name: 'VBtn' })
      .find((item) => item.text().includes('确认变更')).trigger('click')
    await flushPromises()

    expect(api).toHaveBeenCalledWith('/api/admin/users/2/admin', {
      admin: true,
      body: { is_admin: true, reason: '授权管理测试' },
      method: 'PATCH',
    })
    wrapper.unmount()
  })
})
