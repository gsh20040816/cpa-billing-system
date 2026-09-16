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
    upstream_groups: [{ id: 1, name: 'default', is_default: true, gradient_rule_id: 9, gradient_rule: 'default', account_count: 0 }],
    account_configs: [],
    keys: [],
    ownership: [],
    pricing: [{ id: 1, name: 'cpamp-initial', status: 'active', source: 'CPAMP', activated_at: '2026-07-11T12:00:00+08:00' }],
    pricing_rules: {
      active_version: { id: 1, name: 'cpamp-initial', source: 'CPAMP', activated_at: '2026-07-11T12:00:00+08:00' },
      models: [{
        model: 'gpt-test',
        default: {
          input: { usd_per_million: '1' }, output: { usd_per_million: '6' },
          cache_read: { usd_per_million: '0.1' }, cache_creation: { usd_per_million: '1.25' },
        },
        priority: { input: null, output: null, cache_read: null, cache_creation: null },
        flex: { input: null, output: null },
        long_context: { threshold_tokens: null, input_multiplier_ppm: 1000000, output_multiplier_ppm: 1000000 },
      }],
    },
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
        group_id: 1,
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
        group_id: 1,
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

  it('edits a model pricing rule from the billing rules panel', async () => {
    const wrapper = mount(AdminView, {
      attachTo: document.body,
      global: { plugins: [vuetify] },
    })
    await flushPromises()

    const rulesTab = wrapper.findAllComponents({ name: 'VTab' })
      .find((item) => item.text().includes('计费规则'))
    await rulesTab.trigger('click')
    await flushPromises()

    const editButton = wrapper.findAllComponents({ name: 'VBtn' })
      .find((item) => item.text().trim() === '调整')
    expect(editButton).toBeTruthy()
    await editButton.trigger('click')
    await flushPromises()

    const dialog = wrapper.findAllComponents({ name: 'VDialog' })
      .find((item) => item.props('modelValue') === true)
    expect(dialog).toBeTruthy()
    const input = dialog.findAllComponents({ name: 'VTextField' })
      .find((item) => item.props('label') === 'Default Input')
    const reason = dialog.findAllComponents({ name: 'VTextarea' })
      .find((item) => item.props('label') === '变更原因')
    await input.setValue('2')
    await reason.setValue('手动调价测试')
    await dialog.findAllComponents({ name: 'VBtn' })
      .find((item) => item.text().includes('保存并后台重算')).trigger('click')
    await flushPromises()

    expect(api).toHaveBeenCalledWith('/api/admin/pricing-rules', expect.objectContaining({
      admin: true,
      method: 'PUT',
      body: expect.objectContaining({
        model: 'gpt-test',
        input_usd_per_million: '2',
        reason: '手动调价测试',
      }),
    }))
    wrapper.unmount()
  })

  it('shows upstream reset-credit expiries and requires the extra confirmation when the week is not exhausted', async () => {
    const previousAccounts = snapshot.accounts
    snapshot.accounts = {
      accounts: [{
        id: 'account-1',
        name: 'Shared Pro',
        type: 'codex',
        plan_type: 'pro',
        can_refresh: true,
        usage: { requests: 1, total_tokens: 100, cost: '1.0000' },
        quota: [{ key: 'weekly', label: '周额度', used_percent: 81, reset_at: '2026-07-20T03:02:00+08:00' }],
        reset_credits_available: 1,
        reset_credits: [{ id: 'credit-1', expires_at: '2026-08-01T03:56:22Z' }],
        quota_reset_guard: {
          weekly_exhausted: false,
          required_confirmations: 3,
          cpa_status: 'active',
          weekly_windows: [{ key: 'weekly', label: '周额度', used_percent: 81, reset_at: '2026-07-20T03:02:00+08:00' }],
        },
      }],
    }
    const wrapper = mount(AdminView, {
      attachTo: document.body,
      global: { plugins: [vuetify] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('重置上游额度')
    expect(wrapper.text()).toContain('主动重置过期')
    expect(wrapper.text()).toContain('2026/08/01')
    const resetButton = wrapper.findAllComponents({ name: 'VBtn' })
      .find((item) => item.text().includes('重置上游额度'))
    await resetButton.trigger('click')
    await flushPromises()

    expect(document.body.textContent).toContain('第三次确认')
    expect(document.body.textContent).toContain('消耗主动重置次数')
    wrapper.unmount()
    snapshot.accounts = previousAccounts
  })

  it('does not offer Codex quota reset for xAI OAuth accounts', async () => {
    const previousAccounts = snapshot.accounts
    snapshot.accounts = {
      accounts: [{
        id: 'xai-account',
        name: 'gsh@example.com',
        type: 'xai',
        auth_type: 'oauth',
        can_refresh: true,
        usage: { requests: 1, total_tokens: 100, cost: '1.0000' },
        quota: [{ key: 'xai.credit_usage', label: 'OAuth 用量 · 周', used_percent: 13, reset_at: '2026-09-23T16:17:07+08:00' }],
        reset_credits_available: null,
        reset_credits: [],
      }],
    }
    const wrapper = mount(AdminView, {
      attachTo: document.body,
      global: { plugins: [vuetify] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('OAuth 用量')
    expect(wrapper.text()).not.toContain('重置上游额度')
    expect(wrapper.text()).not.toContain('无可用主动重置次数')
    wrapper.unmount()
    snapshot.accounts = previousAccounts
  })

  it('creates a cycle from configured upstream accounts', async () => {
    const previousAccounts = snapshot.accounts
    const previousGradients = snapshot.admin.gradients
    const previousConfigs = snapshot.admin.account_configs
    snapshot.accounts = { accounts: [
      { id: 'oauth-1', name: 'Team OAuth', auth_type: 'oauth', usage: {} },
      { id: 'api-1', name: 'Paid API', auth_type: 'api_key', usage: {} },
    ] }
    snapshot.admin.gradients = [{ id: 9, name: 'default', active: true }]
    snapshot.admin.account_configs = [
      { account_id: 'oauth-1', group_id: 1, group_name: 'default', subscription_mode: 'recurring', period_start: '2026-07-01T00:00:00+08:00', period_cost: '20.00' },
      { account_id: 'api-1', group_id: 1, group_name: 'default', rate: '7.123456' },
    ]
    const wrapper = mount(AdminView, {
      attachTo: document.body,
      global: { plugins: [vuetify] },
    })
    await flushPromises()

    const cyclesTab = wrapper.findAllComponents({ name: 'VTab' })
      .find((item) => item.text().trim() === '账期')
    await cyclesTab.trigger('click')
    await flushPromises()
    await wrapper.findAllComponents({ name: 'VBtn' })
      .find((item) => item.text().includes('创建账期')).trigger('click')
    await flushPromises()
    const dialog = wrapper.findAllComponents({ name: 'VDialog' })
      .find((item) => item.props('modelValue') === true)
    const fields = dialog.findAllComponents({ name: 'VTextField' })
    await fields.find((item) => item.props('label') === '名称').setValue('new-cycle')
    await fields.find((item) => item.props('label') === '开始时间').setValue('2026-07-01T00:00')
    await fields.find((item) => item.props('label') === '结束时间').setValue('2026-08-01T00:00')
    expect(document.body.textContent).toContain('循环')
    await dialog.findAllComponents({ name: 'VBtn' })
      .find((item) => item.text().trim() === '创建').trigger('click')
    await flushPromises()

    expect(api).toHaveBeenCalledWith('/api/admin/cycles', {
      admin: true,
      body: expect.objectContaining({
        name: 'new-cycle',
        upstream_costs: [
          { account_id: 'oauth-1' },
          { account_id: 'api-1' },
        ],
      }),
      method: 'POST',
    })
    wrapper.unmount()
    snapshot.accounts = previousAccounts
    snapshot.admin.gradients = previousGradients
    snapshot.admin.account_configs = previousConfigs
  })

  it('saves an upstream API key rate on the account billing config', async () => {
    const previousAccounts = snapshot.accounts
    const previousCycles = snapshot.admin.cycles
    const previousGradients = snapshot.admin.gradients
    const previousConfigs = snapshot.admin.account_configs
    snapshot.accounts = { accounts: [{
      id: 'codex-api-key:auth-1', name: 'Codex API key sk-paid...1234',
      auth_type: 'api_key', type: 'codex-api-key', usage: {}, can_refresh: false,
    }] }
    snapshot.admin.cycles = [{
      id: 3,
      name: 'current-cycle',
      status: 'open',
      gradient_rule_id: 9,
      upstream_costs: [{
        account_id: 'codex-api-key:auth-1', account_name: 'Codex API key sk-paid...1234',
        auth_type: 'api_key', fixed_cost: null, rate: '7.000000',
      }],
    }]
    snapshot.admin.gradients = [{ id: 9, name: 'default', active: true }]
    snapshot.admin.account_configs = [{
      account_id: 'codex-api-key:auth-1', group_id: 1, group_name: 'default', rate: '7.000000',
    }]
    const wrapper = mount(AdminView, {
      attachTo: document.body,
      global: { plugins: [vuetify] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('current-cycle')
    expect(wrapper.text()).toContain('7.000000 ¥/USD')
    const configureButton = wrapper.findAllComponents({ name: 'VBtn' })
      .find((item) => item.text().includes('配置计费'))
    await configureButton.trigger('click')
    await flushPromises()

    const dialog = wrapper.findAllComponents({ name: 'VDialog' })
      .find((item) => item.props('modelValue') === true)
    const rate = dialog.findAllComponents({ name: 'VTextField' })
      .find((item) => item.props('label') === '人民币 / USD 费率')
    const reason = dialog.findAllComponents({ name: 'VTextarea' })
      .find((item) => item.props('label') === '修改原因')
    expect(rate.props('modelValue')).toBe('7.000000')
    await rate.setValue('8.25')
    await reason.setValue('更新上游 API key 费率')
    await dialog.findAllComponents({ name: 'VBtn' })
      .find((item) => item.text().includes('保存')).trigger('click')
    await flushPromises()

    expect(api).toHaveBeenCalledWith('/api/admin/accounts/codex-api-key%3Aauth-1/billing', {
      admin: true,
      body: {
        group_id: 1,
        reason: '更新上游 API key 费率',
        rate: '8.25',
      },
      method: 'PUT',
    })
    wrapper.unmount()
    snapshot.accounts = previousAccounts
    snapshot.admin.cycles = previousCycles
    snapshot.admin.gradients = previousGradients
    snapshot.admin.account_configs = previousConfigs
  })
})
