// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { Composer } from '../components/Composer'
import { OwnerNote } from '../components/MessageItem'
import { useStore } from '../store'
import type { QueuedMessage } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let host: HTMLDivElement
let root: Root
let copied: string[]
beforeEach(() => {
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  copied = []
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async (t: string) => { copied.push(t) } } })
})
afterEach(() => {
  act(() => root.unmount()); host.remove(); vi.restoreAllMocks()
  useStore.setState({ queued: [], live: null, activeChatId: null })
})

const waiting = (id: string, text: string): QueuedMessage => ({ id, chat_id: 'c1', text, created_at: '2026-11-22T00:00:00Z' })
const LONG = Array.from({ length: 40 }, (_, i) => `line ${i + 1} of a long instruction for the agent`).join('\n')
const show = (queued: QueuedMessage[], busy = true) => {
  useStore.setState({ activeChatId: 'c1', queued, live: busy ? { runId: 'r1', chatId: 'c1', target: null, steps: [] } : null })
  act(() => { root.render(createElement(Composer)) })
}
const items = () => [...host.querySelectorAll('.queued-item')]
const button = (within: Element, name: string) => [...within.querySelectorAll('button')].find((b) => b.textContent === name || b.getAttribute('aria-label') === name)!
const flush = async () => { await act(async () => { await Promise.resolve() }); await act(async () => { await Promise.resolve() }) }

describe('a message that waits for the agent', () => {
  it('is shown whole when it is short', () => {
    show([waiting('q1', 'skip host b')])
    expect(items()[0].querySelector('.queued-text')!.textContent).toBe('skip host b')
    expect(button(items()[0], 'Show all')).toBeUndefined()
  })

  it('is folded when it is long, so that it does not take the screen, and unfolds when asked', () => {
    show([waiting('q1', LONG)])
    const text = () => items()[0].querySelector('.queued-text')!.textContent ?? ''
    expect(text().split('\n').length).toBeLessThanOrEqual(4)
    expect(text()).toContain('line 1 of a long instruction')
    expect(text()).not.toContain('line 9 of')
    expect(items()[0].textContent).toContain('40 lines')
    act(() => { button(items()[0], 'Show all').click() })
    expect(text()).toBe(LONG)
    act(() => { button(items()[0], 'Show less').click() })
    expect(text()).not.toContain('line 9 of')
  })

  it('is folded when it is one very long line', () => {
    show([waiting('q1', 'x'.repeat(5000))])
    expect(items()[0].querySelector('.queued-text')!.textContent!.length).toBeLessThan(400)
    expect(items()[0].textContent).toContain('5,000 characters')
  })

  it('can be copied whole, also while it is folded', async () => {
    show([waiting('q1', LONG)])
    await act(async () => { button(items()[0], 'Copy').click() })
    await flush()
    expect(copied).toEqual([LONG])
    expect(button(items()[0], 'Copied')).toBeDefined()
  })

  it('goes back into the message box to be changed, and is taken off the list', async () => {
    const removed = vi.spyOn(api, 'removeQueue').mockResolvedValue(undefined)
    show([waiting('q1', 'skip host b'), waiting('q2', 'and keep it quiet')])
    await act(async () => { button(items()[0], 'Edit').click() })
    await flush()
    expect(removed).toHaveBeenCalledWith('c1', 'q1')
    expect(host.querySelector('textarea')!.value).toBe('skip host b')
    expect(items().map((i) => i.querySelector('.queued-text')!.textContent)).toEqual(['and keep it quiet'])
  })

  it('does not overwrite what is being typed: it is put behind it', async () => {
    vi.spyOn(api, 'removeQueue').mockResolvedValue(undefined)
    show([waiting('q1', 'skip host b')])
    const area = host.querySelector('textarea')!
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(area, 'half a thought')
      area.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => { button(items()[0], 'Edit').click() })
    await flush()
    expect(area.value).toBe('half a thought\n\nskip host b')
  })

  it('can still be removed', async () => {
    const removed = vi.spyOn(api, 'removeQueue').mockResolvedValue(undefined)
    show([waiting('q1', 'skip host b')])
    await act(async () => { button(items()[0], 'Remove this waiting message').click() })
    await flush()
    expect(removed).toHaveBeenCalledWith('c1', 'q1')
    expect(items()).toHaveLength(0)
  })
})

describe('a message the agent read during its work', () => {
  it('can be copied', async () => {
    act(() => { root.render(createElement(OwnerNote, { text: 'skip host b,\nit is out of scope' })) })
    expect(host.querySelector('.bubble')!.textContent).toBe('skip host b,\nit is out of scope')
    await act(async () => { button(host, 'Copy').click() })
    await flush()
    expect(copied).toEqual(['skip host b,\nit is out of scope'])
  })

  it('can be sent again: it waits for the agent while a run is going, and is a message of its own otherwise', async () => {
    const queue = vi.fn().mockResolvedValue(true)
    useStore.setState({ queueMessage: queue })
    act(() => { root.render(createElement(OwnerNote, { text: 'skip host b' })) })
    await act(async () => { button(host, 'Send again').click() })
    await flush()
    expect(queue).toHaveBeenCalledWith('skip host b')
  })
})
