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
  return selectedRate(item, field, tier)
}

export function priceSourceText(item) {
  const missing = Object.entries(item.configured || {})
    .filter(([, configured]) => !configured)
    .map(([field]) => field)
  if (!missing.length) return '上游完整价格'

  return `${missing.map((field) => priceFieldNames[field]).join('、')} 上游未配置，保留 CPAMP 价格表数值`
}
