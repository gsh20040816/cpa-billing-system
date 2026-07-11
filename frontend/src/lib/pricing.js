const priceFieldNames = {
  input: 'Input',
  output: 'Output',
  cache_read: 'Cache read',
  cache_creation: 'Cache creation',
}

function selectedRate(item, field, tier) {
  if (tier === 'default') return item.default[field]
  if (tier === 'priority') return item.priority[field] || item.default[field]
  if (field === 'cache_read' || field === 'cache_creation') return item.default[field]
  return item.flex[field] || item.default[field]
}

export function effectiveRate(item, field, tier = 'default') {
  const rate = selectedRate(item, field, tier)
  if (
    field === 'cache_creation'
    && !item.configured?.cache_creation
    && Number(rate?.usd_per_million || 0) === 0
  ) {
    return selectedRate(item, 'input', tier)
  }
  return rate
}

export function priceSourceText(item) {
  const missing = Object.entries(item.configured || {})
    .filter(([, configured]) => !configured)
    .map(([field]) => field)
  if (!missing.length) return '上游完整价格'

  const parts = []
  if (missing.includes('cache_creation')) {
    parts.push('Cache creation 未提供，按 Input 价回退')
  }
  const remaining = missing.filter((field) => field !== 'cache_creation')
  if (remaining.length) {
    parts.push(`${remaining.map((field) => priceFieldNames[field]).join('、')} 使用兼容价`)
  }
  return parts.join('；')
}
