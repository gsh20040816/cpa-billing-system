// @vitest-environment jsdom

import { defineComponent, nextTick } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useAutoRefresh } from './autoRefresh'

describe('useAutoRefresh', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('runs immediately, refreshes visible pages, and does not overlap requests', async () => {
    vi.useFakeTimers()
    let resolveTask
    const task = vi.fn(() => new Promise((resolve) => { resolveTask = resolve }))
    const wrapper = mount(defineComponent({ setup: () => { useAutoRefresh(task, { interval: 1000 }); return () => null } }))
    await nextTick()
    expect(task).toHaveBeenCalledWith(false)
    await vi.advanceTimersByTimeAsync(1000)
    expect(task).toHaveBeenCalledTimes(1)
    resolveTask()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    expect(task).toHaveBeenCalledWith(true)
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(3000)
    expect(task).toHaveBeenCalledTimes(2)
  })
})
