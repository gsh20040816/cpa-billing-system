const state = {
  userCsrf: null,
  adminCsrf: null,
}

let sessionRedirecting = false

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
  }
}

export function setCsrf(token, admin = false) {
  state[admin ? 'adminCsrf' : 'userCsrf'] = token || null
}

export function clearCsrf(admin = false) {
  setCsrf(null, admin)
}

function isProtectedPath(path) {
  return typeof path === 'string' && path.startsWith('/api/')
}

function redirectAfterSessionExpiry() {
  clearCsrf()
  clearCsrf(true)
  if (sessionRedirecting || typeof window === 'undefined' || window.location.pathname === '/login') return
  sessionRedirecting = true
  window.location.replace('/login')
}

export async function api(path, options = {}) {
  const { admin = false, body, headers = {}, ...requestOptions } = options
  const method = (requestOptions.method || (body === undefined ? 'GET' : 'POST')).toUpperCase()
  const finalHeaders = { Accept: 'application/json', ...headers }
  if (body !== undefined) finalHeaders['Content-Type'] = 'application/json'
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = admin
      ? (state.adminCsrf || state.userCsrf)
      : (state.userCsrf || state.adminCsrf)
    if (csrf) finalHeaders['X-CSRF-Token'] = csrf
  }
  const response = await fetch(path, {
    ...requestOptions,
    method,
    credentials: 'same-origin',
    headers: finalHeaders,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const contentType = response.headers.get('content-type') || ''
  const payload = contentType.includes('application/json') ? await response.json() : null
  if (!response.ok) {
    const detail = payload?.error || payload?.detail
    const message = typeof detail === 'string' ? detail : `请求失败（HTTP ${response.status}）`
    if (response.status === 401 && isProtectedPath(path)) redirectAfterSessionExpiry()
    throw new ApiError(message, response.status, payload)
  }
  if (path === '/auth/api-key/login' || path === '/auth/admin/login') sessionRedirecting = false
  return payload
}

export async function loadUserSession() {
  const session = await api('/api/session')
  setCsrf(session.csrf_token)
  return session
}

export async function loadAdminSession() {
  const session = await api('/api/admin/session', { admin: true })
  setCsrf(session.csrf_token, true)
  return session
}
