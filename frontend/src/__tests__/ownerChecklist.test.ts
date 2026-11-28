// @vitest-environment happy-dom
import { act, createElement, Fragment } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { GoalPanel } from '../components/GoalPanel'
import { GoalStrip } from '../components/GoalStrip'
import * as dialog from '../dialog'
import { useStore } from '../store'
import { ownerItems, ownerLines } from '../tree'
import type { Chat, Goal } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const GOAL: Goal = { chat_id: 'c1', objective: 'map the login', criteria: [], open_items: ['read the changelog', 'find the form'], done_items: ['count the lines'], owner_items: ['count the lines', 'read the changelog'], updated_at: '2026-11-28T10:00:00Z' }

describe('what the owner types for his checklist', () => {
  it('is an item a line: spacing as one space, no empty line, none twice', () => {
    expect(ownerLines('  read the   changelog \n\ncount the lines\r\nread the changelog\n   ')).toEqual(['read the changelog', 'count the lines'])
    expect(ownerLines('')).toEqual([])
  })
  it('lists his items in the order he wrote them, each open or done', () => {
    expect(ownerItems(GOAL)).toEqual([{ item: 'count the lines', done: true }, { item: 'read the changelog', done: false }])
    expect(ownerItems({ ...GOAL, owner_items: undefined })).toEqual([])
    expect(ownerItems(null)).toEqual([])
  })
})

describe('the owner\'s checklist on screen', () => {
  let host: HTMLDivElement
  let root: Root
  let asked: ReturnType<typeof vi.spyOn>
  let now: Goal | null
  beforeEach(() => {
    host = document.createElement('div'); document.body.append(host); root = createRoot(host)
    now = GOAL
    asked = vi.spyOn(api, 'getGoal').mockImplementation(async () => now)
    vi.spyOn(api, 'listFindings').mockImplementation(async () => [])
    useStore.setState({ chat: { id: 'c1', target_id: null } as Chat, live: null, runDetail: null, error: null })
  })
  afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks(); useStore.setState({ chat: null, error: null }) })

  const show = async () => {
    await act(async () => { root.render(createElement(Fragment, null, createElement(GoalStrip), createElement(GoalPanel, { onClose: () => {} }))) })
    await act(async () => {})
  }
  const strip = () => document.querySelector('.goal-strip')!
  const box = () => document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Items for your checklist, one a line"]')!
  const button = (label: string) => [...document.querySelectorAll('button')].find((b) => (b.getAttribute('aria-label') ?? b.textContent) === label)!
  const type = (text: string) => act(() => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(box(), text)
    box().dispatchEvent(new Event('input', { bubbles: true }))
  })
  const click = async (b: Element) => { await act(async () => { b.dispatchEvent(new MouseEvent('click', { bubbles: true })) }); await act(async () => {}) }

  it('marks in the strip which items are the owner\'s, and counts his open ones', async () => {
    await show()
    const rows = [...strip().querySelectorAll('.goal-item')].map((r) => r.textContent)
    expect(rows.find((r) => r!.includes('read the changelog'))).toContain('yours')
    expect(rows.find((r) => r!.includes('count the lines'))).toContain('yours')
    expect(rows.find((r) => r!.includes('find the form'))).not.toContain('yours')
    expect(strip().querySelector('.goal-head')!.textContent).toContain('2 open (1 yours) · 1 done')
  })

  it('adds what was typed, an item a line, and the strip asks for the record again', async () => {
    const added: Goal = { ...GOAL, open_items: ['read the changelog', 'look at the footer', 'find the form'], owner_items: [...GOAL.owner_items!, 'look at the footer'] }
    const add = vi.spyOn(api, 'addGoalItems').mockImplementation(async () => { now = added; return added })
    await show()
    expect(button('Add to the checklist').disabled).toBe(true)                  // nothing typed yet
    const before = asked.mock.calls.length
    type(' look at   the footer \n\n')
    await click(button('Add to the checklist'))
    expect(add).toHaveBeenCalledWith('c1', ['look at the footer'])
    expect(box().value).toBe('')
    expect(asked.mock.calls.length).toBeGreaterThan(before)
    expect(strip().textContent).toContain('look at the footer')
    expect(button('Take off: look at the footer')).toBeTruthy()
  })

  it('keeps what was typed when it is not taken, and says why', async () => {
    vi.spyOn(api, 'addGoalItems').mockImplementation(async () => { throw new Error('a chat\'s checklist holds 30 items: it has 30, and this would add 1') })
    await show()
    type('one more')
    await click(button('Add to the checklist'))
    expect(box().value).toBe('one more')
    expect(useStore.getState().error).toContain('holds 30 items')
  })

  it('takes an item off when the owner says so, and shows a record that went with it as gone', async () => {
    const remove = vi.spyOn(api, 'removeGoalItem').mockImplementation(async () => { now = null; return null })
    await show()
    await click(button('Take off: read the changelog'))
    expect(remove).toHaveBeenCalledWith('c1', 'read the changelog')
    expect(document.querySelector('.goal-strip')).toBeNull()
    expect(document.body.textContent).toContain('None yet.')
  })

  it('clears the record and what the owner wrote onto it, and the strip goes with it', async () => {
    vi.spyOn(dialog, 'confirmBox').mockResolvedValue(true)
    const clear = vi.spyOn(api, 'clearGoal').mockImplementation(async () => { now = null })
    await show()
    await click(button('Clear'))
    expect(clear).toHaveBeenCalledWith('c1')
    expect(document.querySelector('.goal-strip')).toBeNull()
  })
})
