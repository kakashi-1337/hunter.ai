import type { RunEvent } from './types'

const KNOWN = new Set(['run_started', 'step_started', 'step_finished', 'text_delta', 'thinking_delta', 'fallback', 'run_finished',
  'tool_call', 'tool_started', 'tool_finished', 'permission_request', 'permission_resolved'])

/** Parse one SSE data payload. Anything that is not a known run event becomes null. */
export function parseEvent(data: string): RunEvent | null {
  try {
    const v = JSON.parse(data)
    if (v && typeof v === 'object' && typeof v.type === 'string' && KNOWN.has(v.type)) return v as RunEvent
  } catch {
    /* malformed line: ignore */
  }
  return null
}

/** How long events are collected before they are handed over together: short enough to read as live text. */
export const APPLY_EVERY_MS = 40

/**
 * Follow a run's events. The server replays the run from its first event on every
 * connection, so a reconnect would duplicate deltas: the stream is closed on the
 * first error and `onLost` lets the caller reload the chat from the server instead.
 *
 * The events are handed over in batches. A model answers with one event per token, and the
 * replay of a long run is thousands of events within a moment: handed over one at a time,
 * the page changed, and drew itself, for each of them. The end of the run is not held back,
 * and what came in before the stream was lost is handed over before `onLost`.
 */
export function streamRun(runId: string, onEvents: (batch: RunEvent[]) => void, onLost: () => void): () => void {
  const es = new EventSource(`/api/runs/${runId}/events`)
  let finished = false
  let waiting: RunEvent[] = []
  let timer: ReturnType<typeof setTimeout> | null = null
  const hand = () => {
    if (timer !== null) clearTimeout(timer)
    timer = null
    const batch = waiting
    waiting = []
    if (batch.length) onEvents(batch)
  }
  es.onmessage = (m) => {
    const e = finished ? null : parseEvent(m.data)
    if (!e) return
    waiting.push(e)
    if (e.type === 'run_finished') {
      finished = true
      es.close()
      hand()
    } else if (timer === null) timer = setTimeout(hand, APPLY_EVERY_MS)
  }
  es.onerror = () => {
    es.close()
    if (finished) return
    hand()
    onLost()
  }
  return () => {
    finished = true
    if (timer !== null) clearTimeout(timer)
    timer = null
    waiting = []
    es.close()
  }
}
