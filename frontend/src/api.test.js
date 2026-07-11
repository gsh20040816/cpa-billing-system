import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, clearCsrf, setCsrf } from './api'

afterEach(() => {
  clearCsrf()
  clearCsrf(true)
  vi.restoreAllMocks()
})

describe('api client', () => {
  it('adds the user CSRF token only to mutating requests', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }))
    setCsrf('user-csrf')
    await api('/api/me/keys/1', { method: 'PATCH', body: { name: 'work' } })
    expect(fetchMock.mock.calls[0][1].headers['X-CSRF-Token']).toBe('user-csrf')
  })

  it('keeps administrator CSRF independent from the user session', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }))
    setCsrf('user-csrf')
    setCsrf('admin-csrf', true)
    await api('/api/admin/cycles', { admin: true, body: {} })
    expect(fetchMock.mock.calls[0][1].headers['X-CSRF-Token']).toBe('admin-csrf')
  })
})
