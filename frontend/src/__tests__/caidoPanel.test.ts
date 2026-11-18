import { describe, expect, it } from 'vitest'
import { caidoListLine } from '../tree'

const idle = { busy: false, filtered: false }

describe('the line under the Caido panel\'s filter', () => {
  it('says how many requests are shown', () => {
    expect(caidoListLine(12, 40, 0, idle)).toBe('Showing the newest 12 of 40.')
  })
  it('says how many media requests were left out, and how to list them', () => {
    expect(caidoListLine(12, 40, 5, idle)).toBe('Showing the newest 12 of 40. 5 media requests (images, fonts, audio, video) left out: tick "Media too" to list them.')
    expect(caidoListLine(1, 2, 1, idle)).toContain(' 1 media request (images')
  })
  it('does not call a history of pictures empty', () => {
    expect(caidoListLine(0, 3, 3, idle)).toBe('Only media was recorded here: 3 media requests (images, fonts, audio, video). Tick "Media too" to list them.')
    expect(caidoListLine(0, 3, 3, { busy: false, filtered: true })).toContain('Only media was recorded here')
  })
  it('says nothing while it is asking, and says which of the two an empty list is', () => {
    expect(caidoListLine(0, 0, 0, { busy: true, filtered: false })).toBe('')
    expect(caidoListLine(0, 0, 0, { busy: false, filtered: true })).toBe('Nothing matches this filter.')
    expect(caidoListLine(0, 0, 0, idle)).toContain('Nothing recorded yet.')
    expect(caidoListLine(0, 0, 0, idle)).toContain('of commands sent through Caido')
  })
})
