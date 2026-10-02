import { describe, expect, it } from 'vitest'
import { contextPrices, effectiveRate, priceSourceText } from './pricing'

function rate(value) {
  return { usd_per_million: String(value) }
}

describe('pricing display helpers', () => {
  it('shows explicit Flex cache prices including zero', () => {
    const item = { default: { cache_read: rate(1), cache_creation: rate(2) },
      priority: {}, flex: { cache_read: rate(0), cache_creation: rate(3) } }
    expect(effectiveRate(item, 'cache_read', 'flex').usd_per_million).toBe('0')
    expect(effectiveRate(item, 'cache_creation', 'flex').usd_per_million).toBe('3')
  })
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


it('displays fallback context bands only when the service tier has no own prices', () => {
  const band = { service_tier: 'default', threshold_tokens: 100, input: rate(7) }
  const item = { context_tiers: [band], priority: { input: null }, flex: { input: rate(0) } }
  expect(contextPrices(item, 'priority')).toEqual([band])
  expect(contextPrices(item, 'flex')).toEqual([])
  item.priority.input = rate(3)
  expect(contextPrices(item, 'priority')).toEqual([])
  const own = { ...band, service_tier: 'priority', input: rate(4) }
  item.context_tiers.push(own)
  expect(contextPrices(item, 'priority')).toEqual([own])
})
