import { describe, expect, it } from 'vitest'
import { parseEvent } from '../sse'

describe('parseEvent', () => {
  it('parses a known event', () => {
    expect(parseEvent('{"type":"text_delta","text":"hi"}')).toEqual({ type: 'text_delta', text: 'hi' })
  })
  it('returns null for malformed JSON', () => {
    expect(parseEvent('{bad')).toBeNull()
  })
  it('returns null for JSON that is not an event', () => {
    expect(parseEvent('42')).toBeNull()
    expect(parseEvent('null')).toBeNull()
    expect(parseEvent('{"text":"no type"}')).toBeNull()
    expect(parseEvent('{"type":"something_new"}')).toBeNull()
  })
  it('parses the new events', () => {
    const call = { type: 'tool_call', id: 'c1', name: 'execute', arguments: { command: 'id' }, error: null }
    expect(parseEvent(JSON.stringify(call))).toEqual(call)
    for (const type of ['tool_started', 'tool_finished', 'permission_request', 'permission_resolved']) {
      expect(parseEvent(JSON.stringify({ type }))).toEqual({ type })
    }
  })
})
