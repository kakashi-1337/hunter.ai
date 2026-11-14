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

/**
 * Follow a run's events. The server replays the run from its first event on every
 * connection, so a reconnect would duplicate deltas: the stream is closed on the
 * first error and `onLost` lets the caller reload the chat from the server instead.
 */
export function streamRun(runId: string, onEvent: (e: RunEvent) => void, onLost: () => void): () => void {
  const es = new EventSource(`/api/runs/${runId}/events`)
  let finished = false
  es.onmessage = (m) => {
    const e = parseEvent(m.data)
    if (!e) return
    onEvent(e)
    if (e.type === 'run_finished') {
      finished = true
      es.close()
    }
  }
  es.onerror = () => {
    es.close()
    if (!finished) onLost()
  }
  return () => {
    finished = true
    es.close()
  }
}
