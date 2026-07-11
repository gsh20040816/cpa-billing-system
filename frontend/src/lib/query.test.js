import { describe, expect, it } from 'vitest'
import { activeFilterCount, toQuery } from './query'

describe('query helpers', () => {
  it('serializes arrays, booleans, and zero without empty values', () => {
    const result = toQuery({ model: ['gpt-a', 'gpt-b'], failed: false, min: 0, empty: '', missing: null })
    expect(result).toBe('?model=gpt-a&model=gpt-b&failed=false&min=0')
  })

  it('counts only active filters', () => {
    expect(activeFilterCount({ q: '', status: 'all', model: ['gpt-a'], page: 1, failed: false }, ['page'])).toBe(2)
  })
})
