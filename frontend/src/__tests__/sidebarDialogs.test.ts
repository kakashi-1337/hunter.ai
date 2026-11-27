// @vitest-environment happy-dom
import { act, createElement, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { ErrorBoundary } from '../components/ErrorBoundary'
import { Sidebar } from '../components/Sidebar'
import { useStore } from '../store'
import type { Target } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

// Each dialog the sidebar opens fails before it has drawn anything, as one does that is given a list it cannot read.
const { FAILURE, fails } = vi.hoisted(() => {
  const FAILURE = "Cannot read properties of null (reading 'replace')"
  return { FAILURE, fails: (): never => { throw new TypeError(FAILURE) } }
})
vi.mock('../components/FindingsPanel', () => ({ FindingsPanel: fails }))
vi.mock('../components/CaidoPanel', () => ({ CaidoPanel: fails }))
vi.mock('../components/BenchPanel', () => ({ BenchPanel: fails }))
vi.mock('../components/ActivityPanel', () => ({ ActivityPanel: fails }))
vi.mock('../components/NewChatDialog', () => ({ NewChatDialog: fails }))

/** A target as a server from before the scope lists would send it: the dialog that edits it cannot read it. */
const SHOP = { id: 't1', name: 'Shop', notes: '', scope: {}, created_at: '2026-11-27T10:00:00Z' } as unknown as Target
const Guard = ErrorBoundary as unknown as (props: object) => ReactNode

let root: Root
let host: HTMLElement

beforeEach(async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {})        // React reports a caught failure there
  vi.spyOn(api, 'systemStatus').mockResolvedValue({ cpu_temp: null, cpu_limit: null, local_models: [] })
  useStore.setState({ targets: [SHOP], chats: [], searchResults: null })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  // The sidebar stands where the page puts it: inside a part of its own, which shows a failure in the sidebar's place.
  await act(async () => { root.render(createElement(Guard, {}, createElement(Sidebar, { onSettings: () => {} }))) })
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.restoreAllMocks()
})

const click = (el: Element) => act(() => { el.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
const button = (name: RegExp) => [...host.querySelectorAll('button')].find((b) => name.test(b.getAttribute('aria-label') ?? b.textContent ?? ''))
const dialog = () => host.querySelector('[role="dialog"]')

describe('a dialog of the sidebar that fails to draw', () => {
  it.each([
    [/^Findings$/, 'Findings'], [/^Caido$/, 'Caido'], [/^Bench$/, 'Bench'], [/^Activity$/, 'Activity'],
    [/^\+ New chat$/, 'New chat'], [/^Edit Shop$/, 'Edit target'],
  ])('says so in a dialog of its own name and leaves the sidebar standing: %s', (opens, title) => {
    click(button(opens)!)
    expect(host.querySelector('.sidebar')).not.toBeNull()
    expect(button(/^Settings$/)).toBeDefined()
    expect(dialog()?.getAttribute('aria-label')).toBe(title)
    expect(dialog()?.querySelector('[role="alert"]')?.textContent).toContain('This part of the page could not be shown')
    click(button(/^Close$/)!)
    expect(dialog()).toBeNull()
    click(button(opens)!)                                           // and its button opens it again
    expect(dialog()?.getAttribute('aria-label')).toBe(title)
  })

  it('names what failed, for a report', () => {
    click(button(/^Caido$/)!)
    expect(dialog()?.querySelector('[role="alert"]')?.textContent).toContain(FAILURE)
  })
})
