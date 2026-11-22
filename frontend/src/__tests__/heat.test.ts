// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MessageItem } from '../components/MessageItem'
import { coolingNote } from '../kb'
import { useStore } from '../store'
import { heat, heatNote, machineLine, waitingToCool } from '../tree'
import type { Message, Step, SystemStatus } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const COOL: SystemStatus = { cpu_temp: 60, cpu_limit: 90, gpu_temp: 55, gpu_limit: 85, local_models: [] }
const step = (over: Partial<Step> = {}): Step => ({
  id: 's1', run_id: 'r1', seq: 1, kind: 'cooling', parent_step_id: null, name: 'processor at 93 °C, limit 90 °C', status: 'running',
  started_at: '2026-11-22T10:00:00Z', ended_at: null, input_tokens: 0, output_tokens: 0, detail: { part: 'processor', degrees: 93, limit: 90 }, ...over,
})

describe('how hot the machine is, in the line at the bottom of the sidebar', () => {
  it('names the graphics card beside the processor', () => {
    expect(machineLine({ ...COOL, cpu_temp: 63.4, gpu_temp: 71.6 }).text).toBe('CPU 63°C · GPU 72°C · no local model loaded')
  })

  it('leaves the card out where it cannot be read, or is not asked', () => {
    expect(machineLine({ ...COOL, gpu_temp: null }).text).toBe('CPU 60°C · no local model loaded')
    expect(machineLine({ cpu_temp: 60, cpu_limit: 90, local_models: [] }).text).toBe('CPU 60°C · no local model loaded')      // a server from before it read the card
  })

  it('turns red when either part is at its limit, and yellow when either is within ten degrees of it', () => {
    expect(heat(COOL)).toBe('')
    expect(heat({ ...COOL, gpu_temp: 85 })).toBe('bad')
    expect(heat({ ...COOL, gpu_temp: 75 })).toBe('warn')
    expect(heat({ ...COOL, gpu_temp: 74 })).toBe('')
    expect(heat({ ...COOL, cpu_temp: 90 })).toBe('bad')
    expect(heat({ ...COOL, cpu_temp: 81, gpu_temp: 85 })).toBe('bad')                 // the worse of the two
    expect(heat({ ...COOL, gpu_temp: 99, gpu_limit: null })).toBe('')                  // the owner switched the card's limit off
    expect(machineLine({ ...COOL, gpu_temp: 86 }).level).toBe('bad')
  })

  it('says which part is hot, and what waits for it', () => {
    expect(heatNote(COOL)).toBe('')
    expect(heatNote({ ...COOL, cpu_temp: 93 })).toBe('The processor is at 93°C, at or over its limit of 90°C: a local model is not called, and embedding waits, until it has cooled.')
    expect(heatNote({ ...COOL, gpu_temp: 86 })).toBe('The graphics card is at 86°C, at or over its limit of 85°C: a local model is not called, and embedding waits, until it has cooled.')
    expect(heatNote({ ...COOL, gpu_temp: 78 })).toBe('The graphics card is at 78°C, close to its limit of 85°C.')
    expect(heatNote({ ...COOL, cpu_temp: 84, gpu_temp: 86 })).toBe('The graphics card is at 86°C, at or over its limit of 85°C: a local model is not called, and embedding waits, until it has cooled.')
    expect(heatNote({ ...COOL, cpu_temp: 84, gpu_temp: 80 })).toBe('The processor is at 84°C, close to its limit of 90°C. The graphics card is at 80°C, close to its limit of 85°C.')
  })
})

describe('a run that waits for the machine to cool', () => {
  it('says what it waits for while it does', () => {
    expect(waitingToCool([step()])).toBe('Waiting for the machine to cool before the local model is called: processor at 93 °C, limit 90 °C. The run goes on by itself.')
    expect(waitingToCool([step({ name: 'graphics card at 88 °C, limit 85 °C' })])).toContain('graphics card at 88 °C')
  })

  it('says nothing once the wait is over, and nothing about other steps', () => {
    expect(waitingToCool([step({ status: 'done', ended_at: '2026-11-22T10:00:20Z' })])).toBeNull()
    expect(waitingToCool([step({ kind: 'model_call', name: 'ollama/qwen3:8b' })])).toBeNull()
    expect(waitingToCool([])).toBeNull()
  })

  describe('in the answer', () => {
    let host: HTMLDivElement
    let root: Root
    beforeEach(() => { host = document.createElement('div'); document.body.append(host); root = createRoot(host) })
    afterEach(() => { act(() => root.unmount()); host.remove(); useStore.setState({ live: null }) })
    const answer: Message = { id: 'm1', chat_id: 'c1', parent_id: null, role: 'assistant', blocks: [], provider: 'ollama', model: 'qwen3:8b',
      fallback_from: null, status: 'streaming', created_at: '2026-11-22T10:00:00Z', siblings: ['m1'] }

    it('the wait stands where the answer will, and goes when the model is called', async () => {
      useStore.setState({ live: { runId: 'r1', chatId: 'c1', target: 'm1', steps: [step()] } })
      await act(async () => { root.render(createElement(MessageItem, { message: answer })) })
      expect(host.querySelector('[role="status"]')!.textContent).toContain('Waiting for the machine to cool')
      useStore.setState({ live: { runId: 'r1', chatId: 'c1', target: 'm1', steps: [step({ status: 'done', ended_at: '2026-11-22T10:00:20Z' })] } })
      await act(async () => { root.render(createElement(MessageItem, { message: answer })) })
      expect(host.querySelector('[role="status"]')).toBeNull()
    })
  })
})

describe('an embedding job that waits for the machine to cool', () => {
  it('names the part it waits for', () => {
    expect(coolingNote({ cooling: 96.4, cooling_gpu: null })).toBe('cooling down: the processor is at 96°C, embedding goes on when it has dropped')
    expect(coolingNote({ cooling: null, cooling_gpu: 87.6 })).toBe('cooling down: the graphics card is at 88°C, embedding goes on when it has dropped')
    expect(coolingNote({ cooling: 96 })).toBe('cooling down: the processor is at 96°C, embedding goes on when it has dropped')     // a server from before it read the card
  })

  it('says nothing of a job that does not wait', () => {
    expect(coolingNote({ ok: 12 })).toBeNull()
    expect(coolingNote({ cooling: null, cooling_gpu: null })).toBeNull()
  })
})
