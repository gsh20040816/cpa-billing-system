import { describe, expect, it } from 'vitest'
import {
  DEFAULT_TIME_RANGE,
  TIME_RANGE_ITEMS,
  isTimeRangeReady,
  isValidCustomHours,
  normalizedCustomHours,
  timeRangeQuery,
} from './timeRange'

describe('time range helpers', () => {
  it('uses today as the default range', () => {
    expect(DEFAULT_TIME_RANGE.range).toBe('today')
    expect(timeRangeQuery(DEFAULT_TIME_RANGE)).toEqual({ range: 'today', cycle: null, hours: null })
  })

  it('serializes the shared fixed ranges', () => {
    expect(timeRangeQuery({ ...DEFAULT_TIME_RANGE, range: 'today' })).toEqual({ range: 'today', cycle: null, hours: null })
    expect(timeRangeQuery({ ...DEFAULT_TIME_RANGE, range: 'cycle', cycle: '2026-07' })).toEqual({ range: 'cycle', cycle: '2026-07', hours: null })
    expect(TIME_RANGE_ITEMS.map((item) => item.value)).toEqual(expect.arrayContaining(['60m', 'all']))
    expect(timeRangeQuery({ ...DEFAULT_TIME_RANGE, range: '60m' })).toEqual({ range: '60m', cycle: null, hours: null })
    expect(timeRangeQuery({ ...DEFAULT_TIME_RANGE, range: 'all' })).toEqual({ range: 'all', cycle: null, hours: null })
  })

  it('accepts only positive integer custom hours', () => {
    expect(isValidCustomHours('1')).toBe(true)
    expect(isValidCustomHours(24)).toBe(true)
    expect(isValidCustomHours('0')).toBe(false)
    expect(isValidCustomHours('-1')).toBe(false)
    expect(isValidCustomHours('1.5')).toBe(false)
    expect(isValidCustomHours('24h')).toBe(false)
    expect(normalizedCustomHours('48')).toBe(48)
    expect(normalizedCustomHours('')).toBeNull()
    expect(isTimeRangeReady({ range: 'custom', custom_hours: '6' })).toBe(true)
    expect(isTimeRangeReady({ range: 'custom', custom_hours: '6.5' })).toBe(false)
  })
})
