import { describe, expect, it } from 'vitest'
import { effortChoices } from '../options'

describe('effort choices of a model', () => {
  it('are the ones the model takes, or the usual five when it does not say', () => {
    expect(effortChoices([], undefined)).toEqual(['low', 'medium', 'high', 'xhigh', 'max'])
    expect(effortChoices(undefined, undefined)).toEqual(['low', 'medium', 'high', 'xhigh', 'max'])
    expect(effortChoices(['low', 'high'], undefined)).toEqual(['low', 'high'])
  })
  it('keep showing a value the chat already has, so the control never looks unset', () => {
    expect(effortChoices(['low', 'high'], 'xhigh')).toEqual(['low', 'high', 'xhigh'])
    expect(effortChoices(['low', 'high'], 'high')).toEqual(['low', 'high'])
  })
})
