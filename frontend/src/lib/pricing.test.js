import { describe, expect, it } from 'vitest'
import { effectiveRate, priceSourceText } from './pricing'

function rate(value) {
  return { usd_per_million: String(value) }
}

describe('pricing display helpers', () => {
  it('shows the input-price fallback used for missing cache creation prices', () => {
    const item = {
      default: { input: rate(5), cache_creation: rate(0) },
      priority: { input: rate(10), cache_creation: rate(0) },
      flex: {},
      configured: { input: true, output: true, cache_read: true, cache_creation: false },
    }

    expect(effectiveRate(item, 'cache_creation').usd_per_million).toBe('5')
    expect(effectiveRate(item, 'cache_creation', 'priority').usd_per_million).toBe('10')
    expect(priceSourceText(item)).toBe('Cache creation 未提供，按 Input 价回退')
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
