import { describe, expect, it } from 'vitest'
import { effectiveRate, priceSourceText } from './pricing'

function rate(value) {
  return { usd_per_million: String(value) }
}

describe('pricing display helpers', () => {
  it('preserves CPAMP zero prices for unconfigured cache creation', () => {
    const item = {
      default: { input: rate(5), cache_creation: rate(0) },
      priority: { input: rate(10), cache_creation: rate(0) },
      flex: {},
      configured: { input: true, output: true, cache_read: true, cache_creation: false },
    }

    expect(effectiveRate(item, 'cache_creation').usd_per_million).toBe('0')
    expect(effectiveRate(item, 'cache_creation', 'priority').usd_per_million).toBe('0')
    expect(priceSourceText(item)).toBe('Cache creation 上游未配置，保留 CPAMP 价格表数值')
  })

  it('keeps complete upstream prices unchanged', () => {
    const item = {
      default: { input: rate(1), cache_creation: rate(1.25) },
      priority: {},
      flex: {},
      configured: { input: true, output: true, cache_read: true, cache_creation: true },
    }

    expect(effectiveRate(item, 'cache_creation').usd_per_million).toBe('1.25')
    expect(priceSourceText(item)).toBe('上游完整价格')
  })
})
