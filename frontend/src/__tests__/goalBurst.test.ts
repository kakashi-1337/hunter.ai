// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { GoalPanel } from '../components/GoalPanel'
import { GoalStrip } from '../components/GoalStrip'
import { SETTLE_MS } from '../settled'
import { useStore } from '../store'
import type { Chat, Goal, Step } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const GOAL: Goal = { chat_id: 'c1', objective: 'map the login', criteria: [], open_items: ['find the form'], done_items: [], updated_at: '2026-11-22T10:00:00Z' }
const steps = (n: number): Step[] => Array.from({ length: n }, (_, i) => ({
  id: `s${i}`, run_id: 'r1', seq: i + 1, kind: 'tool_call', parent_step_id: null, name: 'read', status: 'done',
  started_at: '2026-11-22T10:00:00Z', ended_at: '2026-11-22T10:00:01Z', input_tokens: 0, output_tokens: 0, detail: {},
}))
const live = (n: number) => ({ runId: 'r1', chatId: 'c1', target: null, steps: steps(n) })

let host: HTMLDivElement
let root: Root
let goal: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  vi.useFakeTimers()
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  goal = vi.spyOn(api, 'getGoal').mockImplementation(async () => GOAL)
  vi.spyOn(api, 'listFindings').mockImplementation(async () => [])
  useStore.setState({ chat: { id: 'c1', target_id: null } as Chat, live: live(0), runDetail: null })
})
afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks(); vi.useRealTimers(); useStore.setState({ chat: null, live: null }) })

const settle = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
/** Steps that come one close behind the other, two milliseconds apart. */
const burst = async (from: number, to: number) => {
  for (let n = from; n <= to; n++) { act(() => { useStore.setState({ live: live(n) }) }); await settle(2) }
}

describe.each([
  ['the goal strip above the messages', () => createElement(GoalStrip)],
  ['the goal panel', () => createElement(GoalPanel, { onClose: () => {} })],
])('%s, while a run adds steps', (_, element) => {
  it('asks for the goal at once when a chat is opened', async () => {
    await act(async () => { root.render(element()) })
    await settle(0)
    expect(goal).toHaveBeenCalledTimes(1)
    expect(goal).toHaveBeenCalledWith('c1')
  })

  it('asks once after a burst of steps, not once for each step', async () => {
    await act(async () => { root.render(element()) })
    await settle(0)
    await burst(1, 150)                                     // the replay of a run: its steps within a moment
    await settle(SETTLE_MS - 3)
    expect(goal).toHaveBeenCalledTimes(1)                   // still only the first, though the burst took longer than the wait
    await settle(1)
    expect(goal).toHaveBeenCalledTimes(2)
  })

  it('asks again for each step that comes by itself', async () => {
    await act(async () => { root.render(element()) })
    await settle(0)
    await burst(1, 1); await settle(SETTLE_MS)
    await burst(2, 2); await settle(SETTLE_MS)
    expect(goal).toHaveBeenCalledTimes(3)
  })
})
