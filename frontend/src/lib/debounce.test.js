import { afterEach, describe, expect, it, vi } from 'vitest'
import { createDebouncedTask } from './debounce'

afterEach(() => {
  vi.useRealTimers()
})

describe('createDebouncedTask', () => {
  it('coalesces rapid updates and keeps the latest arguments', async () => {
    vi.useFakeTimers()
    const task = vi.fn()
    const debounced = createDebouncedTask(task, 200)

    debounced.schedule('first')
    debounced.schedule('latest')
    await vi.advanceTimersByTimeAsync(199)
    expect(task).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(task).toHaveBeenCalledOnce()
    expect(task).toHaveBeenCalledWith('latest')
  })

  it('runs immediately and cancels a pending update', async () => {
    vi.useFakeTimers()
    const task = vi.fn()
    const debounced = createDebouncedTask(task, 200)

    debounced.schedule('pending')
    debounced.run('now')
    await vi.runAllTimersAsync()
    expect(task).toHaveBeenCalledOnce()
    expect(task).toHaveBeenCalledWith('now')
  })
})
