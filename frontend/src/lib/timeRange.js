export const TIME_RANGE_ITEMS = [
  { title: '今天', value: 'today' },
  { title: '近60min', value: '60m' },
  { title: '昨天', value: 'yesterday' },
  { title: '24h', value: '24h' },
  { title: '7d', value: '7d' },
  { title: '30d', value: '30d' },
  { title: '当前账期', value: 'cycle' },
  { title: '所有时间', value: 'all' },
  { title: '自定义', value: 'custom' },
]

export const DEFAULT_TIME_RANGE = {
  range: 'today',
  cycle: '',
  custom_hours: '',
}

const RANGE_VALUES = new Set(TIME_RANGE_ITEMS.map((item) => item.value))

export function isValidCustomHours(value) {
  if (typeof value === 'number') return Number.isSafeInteger(value) && value > 0
  return typeof value === 'string' && /^[1-9]\d*$/.test(value.trim()) && Number.isSafeInteger(Number(value))
}

export function normalizedCustomHours(value) {
  return isValidCustomHours(value) ? Number(value) : null
}

export function isTimeRangeReady(value) {
  if (!RANGE_VALUES.has(value?.range)) return false
  return value.range !== 'custom' || isValidCustomHours(value.custom_hours)
}

export function timeRangeQuery(value) {
  const range = RANGE_VALUES.has(value?.range) ? value.range : DEFAULT_TIME_RANGE.range
  return {
    range,
    cycle: range === 'cycle' ? (value?.cycle || null) : null,
    hours: range === 'custom' ? normalizedCustomHours(value?.custom_hours) : null,
  }
}

export function timeRangeLabel(value) {
  const item = TIME_RANGE_ITEMS.find((candidate) => candidate.value === value?.range)
  if (!item) return '24h'
  if (value.range === 'custom' && isValidCustomHours(value.custom_hours)) return `${value.custom_hours}h`
  return item.title
}
