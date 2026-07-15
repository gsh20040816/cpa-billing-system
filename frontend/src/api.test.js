import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, clearCsrf, setCsrf } from './api'

afterEach(() => {
  clearCsrf()
  clearCsrf(true)
  vi.unstubAllGlobals()
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

  it('automatically clears both sessions and redirects on an expired protected session', async () => {
    const redirect = vi.fn()
    vi.stubGlobal('window', { location: { pathname: '/requests', replace: redirect } })
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response(
      JSON.stringify({ detail: '用户会话已失效' }),
      { status: 401, headers: { 'content-type': 'application/json' } },
    ))
    setCsrf('user-csrf')
    setCsrf('admin-csrf', true)

    await expect(api('/api/dashboard')).rejects.toMatchObject({ status: 401 })
    expect(redirect).toHaveBeenCalledOnce()
    expect(redirect).toHaveBeenCalledWith('/login')

    fetchMock.mockResolvedValueOnce(new Response('{}', {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }))
    await api('/api/me/keys/1', { method: 'PATCH', body: { name: 'work' } })
    expect(fetchMock.mock.calls[1][1].headers['X-CSRF-Token']).toBeUndefined()

    fetchMock.mockResolvedValueOnce(new Response('{}', {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }))
    await api('/auth/api-key/login', { body: { api_key: 'key' } })
  })

  it('does not redirect when login credentials are rejected', async () => {
    const redirect = vi.fn()
    vi.stubGlobal('window', { location: { pathname: '/login', replace: redirect } })
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(
      JSON.stringify({ detail: 'API Key 无效' }),
      { status: 401, headers: { 'content-type': 'application/json' } },
    ))

    await expect(api('/auth/api-key/login', { body: { api_key: 'wrong' } })).rejects.toMatchObject({ status: 401 })
    expect(redirect).not.toHaveBeenCalled()
  })
})
