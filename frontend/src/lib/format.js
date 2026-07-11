export function number(value) {
  return new Intl.NumberFormat('zh-CN').format(Number(value || 0))
}

export function compactNumber(value) {
  const amount = Number(value || 0)
  if (Math.abs(amount) < 1000) return String(amount)
  return new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 2 }).format(amount)
}

export function money(value, currency = '$') {
  if (value === null || value === undefined || value === '') return '-'
  return `${currency}${value}`
}

export function dateTime(value) {
  if (!value) return '-'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(parsed)
}

export function duration(value) {
  if (value === null || value === undefined) return '-'
  const ms = Number(value)
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(2)} s`
  return `${(ms / 60000).toFixed(1)} min`
}

export function percent(value, digits = 1) {
  if (value === null || value === undefined) return '-'
  return `${Number(value).toFixed(digits)}%`
}

export function quotaTone(value) {
  const amount = Number(value || 0)
  if (amount >= 90) return 'error'
  if (amount >= 70) return 'warning'
  return 'primary'
}
