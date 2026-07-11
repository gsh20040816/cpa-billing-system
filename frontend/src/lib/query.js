export function toQuery(params) {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value === null || value === undefined || value === '') return
    if (Array.isArray(value)) {
      value.filter(Boolean).forEach((item) => query.append(key, item))
      return
    }
    query.set(key, String(value))
  })
  const text = query.toString()
  return text ? `?${text}` : ''
}

export function activeFilterCount(filters, ignored = []) {
  return Object.entries(filters).filter(([key, value]) => {
    if (ignored.includes(key)) return false
    if (Array.isArray(value)) return value.length > 0
    return value !== null && value !== undefined && value !== '' && value !== 'all'
  }).length
}
