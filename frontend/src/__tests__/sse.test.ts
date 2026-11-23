import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { APPLY_EVERY_MS, parseEvent, streamRun } from '../sse'
import type { RunEvent } from '../types'

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
    for (const type of ['tool_started', 'tool_finished', 'permission_request', 'permission_resolved', 'answer_restarted']) {
      expect(parseEvent(JSON.stringify({ type }))).toEqual({ type })
    }
  })
})

class FakeSource {
  static last: FakeSource
  onmessage: ((m: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  url: string
  constructor(url: string) { this.url = url; FakeSource.last = this }
  close() { this.closed = true }
  say(e: unknown) { this.onmessage?.({ data: typeof e === 'string' ? e : JSON.stringify(e) }) }
}
const delta = (text: string): RunEvent => ({ type: 'text_delta', text })
const END: RunEvent = { type: 'run_finished', status: 'done', error: null, totals: {} as never, message: null }

describe('streamRun: the events of a run reach the page in batches', () => {
  let got: RunEvent[][]
  let lost: number
  let order: string[]
  let stop: () => void
  beforeEach(() => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', FakeSource)
    got = []; lost = 0; order = []
    stop = streamRun('r1', (batch) => { got.push(batch); order.push('events') }, () => { lost += 1; order.push('lost') })
  })
  afterEach(() => { stop(); vi.useRealTimers(); vi.unstubAllGlobals() })
  const src = () => FakeSource.last

  it('asks the server for the events of this run', () => {
    expect(src().url).toBe('/api/runs/r1/events')
  })

  it('a burst is handed over together, in the order it came', () => {
    // The replay of a long run: thousands of events in a moment. One at a time, the page drew itself for each.
    for (let i = 0; i < 500; i++) src().say(delta(`t${i} `))
    expect(got).toEqual([])                                   // the burst is still coming in
    vi.advanceTimersByTime(APPLY_EVERY_MS)
    expect(got.length).toBe(1)
    expect(got[0].map((e) => (e as { text: string }).text).join('')).toBe(Array.from({ length: 500 }, (_, i) => `t${i} `).join(''))
  })

  it('an event that comes alone is handed over within a moment', () => {
    src().say(delta('a'))
    vi.advanceTimersByTime(APPLY_EVERY_MS - 1)
    expect(got).toEqual([])
    vi.advanceTimersByTime(1)
    expect(got).toEqual([[delta('a')]])
    src().say(delta('b'))
    vi.advanceTimersByTime(APPLY_EVERY_MS)
    expect(got).toEqual([[delta('a')], [delta('b')]])
  })

  it('a batch is a whole moment long: an event at the end of one does not cut the next one short', () => {
    src().say(delta('a'))
    vi.advanceTimersByTime(10)
    src().say(delta('b'))
    vi.advanceTimersByTime(10)
    src().say(delta('c'))
    vi.advanceTimersByTime(APPLY_EVERY_MS - 20)
    expect(got).toEqual([[delta('a'), delta('b'), delta('c')]])
    vi.advanceTimersByTime(5)
    src().say(delta('d'))                                     // the next moment starts with it
    vi.advanceTimersByTime(APPLY_EVERY_MS - 1)
    expect(got.length).toBe(1)
    vi.advanceTimersByTime(1)
    expect(got[1]).toEqual([delta('d')])
  })

  it('the end of the run is handed over at once, with what came before it', () => {
    src().say(delta('a')); src().say(END)
    expect(got).toEqual([[delta('a'), END]])                  // not a moment later: what waits for the end starts now
    expect(src().closed).toBe(true)
    vi.advanceTimersByTime(APPLY_EVERY_MS * 3)
    expect(got.length).toBe(1)
    src().onerror!()                                          // the server closing the stream after the end is not a loss
    expect(lost).toBe(0)
  })

  it('what came in before the stream was lost is handed over first', () => {
    src().say(delta('a'))
    src().onerror!()
    expect(order).toEqual(['events', 'lost'])
    expect(got).toEqual([[delta('a')]])
    expect(src().closed).toBe(true)
    vi.advanceTimersByTime(APPLY_EVERY_MS * 3)
    expect(got.length).toBe(1)
  })

  it('nothing is handed over once the caller has stopped listening', () => {
    src().say(delta('a'))
    stop()
    src().say(delta('b'))                                     // one that was already on its way
    vi.advanceTimersByTime(APPLY_EVERY_MS * 3)
    expect(got).toEqual([])
    expect(src().closed).toBe(true)
    src().onerror!()
    expect(lost).toBe(0)
  })

  it('what is not an event is left out of a batch', () => {
    src().say('{bad'); src().say({ type: 'something_new' }); src().say(delta('a'))
    vi.advanceTimersByTime(APPLY_EVERY_MS)
    expect(got).toEqual([[delta('a')]])
  })
})
