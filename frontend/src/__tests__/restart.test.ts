// @vitest-environment happy-dom
import { act, createElement, type ComponentProps, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { ErrorBoundary } from '../components/ErrorBoundary'
import { MachineStatus } from '../components/MachineStatus'
import { DialogGuard, Modal } from '../components/Modal'
import { restartNotice } from '../tree'
import type { SystemStatus } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let root: Root
let host: HTMLElement
let logged: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  logged = vi.spyOn(console, 'error').mockImplementation(() => {})      // React reports a caught failure there
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.restoreAllMocks()
})

const QUIET: SystemStatus = { cpu_temp: null, cpu_limit: 90, local_models: null, restart_needed: false }
const alerts = () => [...host.querySelectorAll('[role="alert"]')].map((a) => a.textContent ?? '')
const button = (name: RegExp) => [...host.querySelectorAll('button')].find((b) => name.test(b.textContent ?? ''))!
const click = (el: Element) => act(() => { el.dispatchEvent(new MouseEvent('click', { bubbles: true })) })

describe('the notice that HunterAI has to be restarted', () => {
  it('is given when the server says that its code changed on disk after it started', () => {
    expect(restartNotice({ ...QUIET, restart_needed: true })).toMatch(/^Restart HunterAI: its code on this machine changed after the server was started/)
  })

  it('is given when the server does not say: a server from before this page was built', () => {
    const { restart_needed: _, ...old } = QUIET
    const text = restartNotice(old as SystemStatus)
    expect(text).toMatch(/^Restart HunterAI: the server that answers is older than this page/)
    expect(text).toContain('scripts/start.sh')
  })

  it('is not given while the server runs the code that is on disk, or does not answer', () => {
    expect(restartNotice(QUIET)).toBeNull()
    expect(restartNotice(null)).toBeNull()
  })

  it('stands above the machine line as an alert once the status says so', async () => {
    const status = vi.spyOn(api, 'systemStatus').mockResolvedValue({ ...QUIET, restart_needed: true })
    await act(async () => { root.render(createElement(MachineStatus)) })
    expect(status).toHaveBeenCalled()
    expect(alerts()).toHaveLength(1)
    expect(alerts()[0]).toContain('Restart HunterAI')
    expect(host.querySelector('.machine')).not.toBeNull()          // the machine line is still there
  })

  it('is absent from the sidebar while nothing has to be done', async () => {
    vi.spyOn(api, 'systemStatus').mockResolvedValue(QUIET)
    await act(async () => { root.render(createElement(MachineStatus)) })
    expect(alerts()).toEqual([])
  })
})

const FAILURE = "Cannot read properties of undefined (reading 'trim')"
let broken = true
function Part({ label = 'the part' }: { label?: string }): ReactNode {
  if (broken) throw new TypeError(FAILURE)
  return createElement('p', { className: 'part' }, label)
}
// createElement takes the children as arguments; the components' own types ask for them among the props.
const Guard = ErrorBoundary as unknown as (props: Omit<ComponentProps<typeof ErrorBoundary>, 'children'>) => ReactNode
const ModalAround = Modal as (props: Omit<ComponentProps<typeof Modal>, 'children'>) => ReactNode
const DialogGuardAround = DialogGuard as (props: Omit<ComponentProps<typeof DialogGuard>, 'children'>) => ReactNode
/** A dialog that fails before it has drawn anything: in its own body, not in what it holds. */
function Dialog({ onClose }: { onClose: () => void }): ReactNode {
  if (broken) throw new TypeError(FAILURE)
  return createElement(ModalAround, { title: 'Settings', onClose }, 'the settings')
}

describe('a part of the page that fails to draw', () => {
  beforeEach(() => { broken = true })

  it('shows what went wrong in its place and leaves the rest of the page standing', () => {
    act(() => root.render(createElement('div', null, createElement('nav', null, 'the sidebar'), createElement(Guard, {}, createElement(Part)))))
    expect(host.querySelector('nav')?.textContent).toBe('the sidebar')
    expect(alerts()).toHaveLength(1)
    expect(alerts()[0]).toContain('This part of the page could not be shown')
    expect(alerts()[0]).toContain(FAILURE)
    expect(alerts()[0]).toMatch(/restart/i)                         // an update without a restart is the usual cause
    expect(button(/Reload the page/)).toBeDefined()
  })

  it('writes the failure to the console, where it can be copied for a report', () => {
    act(() => root.render(createElement(Guard, {}, createElement(Part))))
    expect(logged.mock.calls.some((args: unknown[]) => args.some((a) => a instanceof TypeError && a.message === FAILURE))).toBe(true)
  })

  it('draws the part again when asked to', () => {
    act(() => root.render(createElement(Guard, {}, createElement(Part))))
    broken = false                                                  // what made it fail is gone
    click(button(/Try again/))
    expect(alerts()).toEqual([])
    expect(host.querySelector('.part')?.textContent).toBe('the part')
  })

  it('draws again by itself when it is given something else to show', () => {
    act(() => root.render(createElement(Guard, { resetKey: 'chat-1' }, createElement(Part, { label: 'chat 1' }))))
    expect(alerts()).toHaveLength(1)
    broken = false
    act(() => root.render(createElement(Guard, { resetKey: 'chat-1' }, createElement(Part, { label: 'chat 1' }))))
    expect(alerts()).toHaveLength(1)                                // the same thing: it stays as it is until asked
    act(() => root.render(createElement(Guard, { resetKey: 'chat-2' }, createElement(Part, { label: 'chat 2' }))))
    expect(alerts()).toEqual([])
    expect(host.querySelector('.part')?.textContent).toBe('chat 2')
  })

  it('keeps a dialog open, with its title and its buttons, when what is inside it fails', () => {
    const onClose = vi.fn()
    act(() => root.render(createElement(ModalAround, { title: 'Settings', onClose, footer: createElement('button', { onClick: onClose }, 'Done') }, createElement(Part))))
    const dialog = host.querySelector('[role="dialog"]')!
    expect(dialog.getAttribute('aria-label')).toBe('Settings')
    expect(dialog.querySelector('h2')?.textContent).toBe('Settings')
    expect(dialog.querySelector('[role="alert"]')?.textContent).toContain(FAILURE)
    click(button(/Done/))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('shows a dialog that fails before it draws anything as a dialog of that name, which can be closed', () => {
    const onClose = vi.fn()
    act(() => root.render(createElement('div', null, createElement('nav', null, 'the sidebar'),
      createElement(DialogGuardAround, { title: 'Settings', onClose }, createElement(Dialog, { onClose })))))
    expect(host.querySelector('nav')?.textContent).toBe('the sidebar')
    const dialog = host.querySelector('[role="dialog"]')!
    expect(dialog.getAttribute('aria-label')).toBe('Settings')
    expect(dialog.querySelector('[role="alert"]')?.textContent).toContain(FAILURE)
    click(button(/^Close$/))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('leaves a dialog that draws as it is', () => {
    broken = false
    act(() => root.render(createElement(DialogGuardAround, { title: 'Settings', onClose: () => {} }, createElement(Dialog, { onClose: () => {} }))))
    expect(alerts()).toEqual([])
    expect(host.querySelector('[role="dialog"] .body')?.textContent).toBe('the settings')
  })
})
