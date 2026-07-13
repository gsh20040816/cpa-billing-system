import { describe, expect, it } from 'vitest'
import {
  DEFAULT_TIME_RANGE,
  isTimeRangeReady,
  isValidCustomHours,
  normalizedCustomHours,
  timeRangeQuery,
} from './timeRange'

describe('time range helpers', () => {
  it('serializes the shared fixed ranges', () => {
    expect(timeRangeQuery({ ...DEFAULT_TIME_RANGE, range: 'today' })).toEqual({ range: 'today', cycle: null, hours: null })
    expect(timeRangeQuery({ ...DEFAULT_TIME_RANGE, range: 'cycle', cycle: '2026-07' })).toEqual({ range: 'cycle', cycle: '2026-07', hours: null })
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
