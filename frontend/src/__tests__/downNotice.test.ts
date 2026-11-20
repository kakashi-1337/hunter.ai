// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { MachineStatus } from '../components/MachineStatus'
import { SetupChecklist } from '../components/SetupChecklist'
import { stoppedNotice } from '../tree'
import type { Checkup, CheckupItem, SystemStatus } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let root: Root
let host: HTMLElement
const clipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')

beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.restoreAllMocks()
  if (clipboard) Object.defineProperty(navigator, 'clipboard', clipboard)
  else delete (navigator as { clipboard?: unknown }).clipboard
})

const QUIET: SystemStatus = { cpu_temp: null, cpu_limit: 90, local_models: [], restart_needed: false, stopped: [] }
const SEARX = { id: 'searxng', label: 'SearXNG (web_search)' }
const REA = { id: 'mcp', label: 'MCP server rea' }
const alerts = () => [...host.querySelectorAll('[role="alert"]')].map((a) => a.textContent ?? '')
const button = (name: RegExp) => [...host.querySelectorAll('button')].find((b) => name.test(b.textContent ?? ''))
const click = (el: Element) => act(async () => { el.dispatchEvent(new MouseEvent('click', { bubbles: true })) })

describe('the notice that a part is not working', () => {
  it('names each part that was in use and does not run now', () => {
    expect(stoppedNotice({ ...QUIET, stopped: [SEARX] })).toBe('Not working: SearXNG (web_search).')
    expect(stoppedNotice({ ...QUIET, stopped: [SEARX, REA] })).toBe('Not working: SearXNG (web_search), MCP server rea.')
  })

  it('is not given while all of it runs, the server does not answer, or the server is from before it could tell', () => {
    expect(stoppedNotice(QUIET)).toBeNull()
    expect(stoppedNotice(null)).toBeNull()
    const { stopped: _, ...old } = QUIET
    expect(stoppedNotice(old as SystemStatus)).toBeNull()
  })

  it('stands above the machine line as an alert, with the way to the setup check', async () => {
    const onSetup = vi.fn()
    vi.spyOn(api, 'systemStatus').mockResolvedValue({ ...QUIET, stopped: [SEARX] })
    await act(async () => { root.render(createElement(MachineStatus, { onSetup })) })
    expect(alerts()).toHaveLength(1)
    expect(alerts()[0]).toContain('Not working: SearXNG (web_search).')
    expect(host.querySelector('.machine')).not.toBeNull()
    await click(button(/^Setup check$/)!)
    expect(onSetup).toHaveBeenCalledTimes(1)
  })

  it('is given without a button where nothing can open the setup check', async () => {
    vi.spyOn(api, 'systemStatus').mockResolvedValue({ ...QUIET, stopped: [SEARX] })
    await act(async () => { root.render(createElement(MachineStatus)) })
    expect(alerts()[0]).toContain('Not working: SearXNG (web_search).')
    expect(button(/Setup check/)).toBeUndefined()
  })

  it('is absent while nothing stopped', async () => {
    vi.spyOn(api, 'systemStatus').mockResolvedValue(QUIET)
    await act(async () => { root.render(createElement(MachineStatus, { onSetup: () => {} })) })
    expect(alerts()).toEqual([])
    expect(button(/Setup check/)).toBeUndefined()
  })

  it('can be put away until the part has run again', async () => {
    vi.spyOn(api, 'systemStatus').mockResolvedValue({ ...QUIET, stopped: [SEARX, REA] })
    const hide = vi.spyOn(api, 'hideStopped').mockResolvedValue({ stopped: [] })
    await act(async () => { root.render(createElement(MachineStatus, { onSetup: () => {} })) })
    const away = host.querySelector<HTMLButtonElement>('button[aria-label="Hide this notice"]')!
    expect(away.title).toBe('I know: do not say it again until it has run again. The setup check keeps showing it.')
    await click(away)
    expect(hide).toHaveBeenCalledTimes(1)
    expect(alerts()).toEqual([])
    expect(host.querySelector('.machine')).not.toBeNull()
  })

  it('stays when the server could not be told to put it away', async () => {
    vi.spyOn(api, 'systemStatus').mockResolvedValue({ ...QUIET, stopped: [SEARX] })
    vi.spyOn(api, 'hideStopped').mockRejectedValue(new Error('500: Internal Server Error'))
    await act(async () => { root.render(createElement(MachineStatus)) })
    await click(host.querySelector('button[aria-label="Hide this notice"]')!)
    expect(alerts()).toHaveLength(1)
    expect(alerts()[0]).toContain('Not working: SearXNG (web_search).')
  })

  it('stands under the notice to restart when both hold', async () => {
    vi.spyOn(api, 'systemStatus').mockResolvedValue({ ...QUIET, restart_needed: true, stopped: [REA] })
    await act(async () => { root.render(createElement(MachineStatus, { onSetup: () => {} })) })
    expect(alerts().map((a) => a.slice(0, 16))).toEqual(['Restart HunterAI', 'Not working: MCP'])
  })
})

const item = (id: string, label: string, over: Partial<CheckupItem> = {}): CheckupItem =>
  ({ id, label, ok: true, required: false, detail: 'in place', fix: '', stopped: false, ...over })
const MODEL = item('model', 'A model to talk to', { required: true })
const SEARX_DOWN = item('searxng', 'SearXNG (web_search)', { ok: false, stopped: true, detail: 'no answer at http://127.0.0.1:8888', fix: 'Run `scripts/searxng.sh start` in the HunterAI folder (it needs Docker).' })
const QDRANT_MISSING = item('qdrant', 'Qdrant (the knowledge base)', { ok: false, detail: 'no answer at http://127.0.0.1:6333', fix: 'Run `scripts/qdrant.sh` in the HunterAI folder (it needs Docker).' })
const MCP_DOWN = item('mcp', 'MCP servers (tools from other programs)', { ok: false, stopped: true, detail: 'not working: rea (npx was not found)', fix: 'Open Settings and press "Manage MCP servers".' })
const TEXT = 'HunterAI diagnostics, 2026-11-17 10:00\nVersion: d65353d (2026-11-17) on main\nChecks:\n  STOPPED  SearXNG (web_search): no answer\n'

async function openCheck(check: Checkup): Promise<void> {
  vi.spyOn(api, 'systemCheckup').mockResolvedValue(check)
  await act(async () => { root.render(createElement(SetupChecklist, { onClose: () => {}, onSettings: () => {} })) })
}
const badges = () => [...host.querySelectorAll('.setup-item .badge')].map((b) => b.textContent)
const summary = () => host.querySelector('[role="status"]')?.textContent ?? ''
const copyButton = () => button(/^(Copy diagnostics|Copying…|Copied)$/)!
function clipboardThat(write: (text: string) => Promise<void>): void {
  Object.defineProperty(navigator, 'clipboard', { value: { writeText: write }, configurable: true })
}

describe('the setup check', () => {
  it('tells a part that stopped working from one that was never there', async () => {
    await openCheck({ ready: true, items: [MODEL, SEARX_DOWN, QDRANT_MISSING] })
    expect(badges()).toEqual(['ready', 'not working', 'optional, missing'])
    expect(host.querySelectorAll('.setup-item .dot.error')).toHaveLength(1)
    expect(summary()).toBe('Not working: SearXNG (web_search). Its line says how to start it again. 1 optional part is missing: the feature named on that line stays off until it is there.')
  })

  it('names every part that stopped, and nothing else when the rest is in place', async () => {
    await openCheck({ ready: true, items: [MODEL, SEARX_DOWN, MCP_DOWN] })
    expect(summary()).toBe('Not working: SearXNG (web_search), MCP servers (tools from other programs). Their lines say how to start them again.')
    expect(host.querySelector('[role="status"]')?.className).toBe('ask-warning')
  })

  it('says what it said before while nothing stopped', async () => {
    await openCheck({ ready: true, items: [MODEL, QDRANT_MISSING] })
    expect(badges()).toEqual(['ready', 'optional, missing'])
    expect(summary()).toBe('Ready to chat. 1 optional part is missing: the feature named on that line stays off until it is there.')
    expect(host.querySelector('[role="status"]')?.className).toBe('hint')
  })

  it('reads the answer of a server from before it could tell what stopped', async () => {
    const { stopped: _, ...old } = QDRANT_MISSING
    await openCheck({ ready: true, items: [MODEL, old as CheckupItem] })
    expect(badges()).toEqual(['ready', 'optional, missing'])
  })

  it('copies the diagnostics and says what they hold', async () => {
    const written: string[] = []
    clipboardThat(async (text) => { written.push(text) })
    const asked = vi.spyOn(api, 'systemDiagnostics').mockResolvedValue({ text: TEXT })
    await openCheck({ ready: true, items: [MODEL, SEARX_DOWN] })
    expect(host.querySelector('textarea')).toBeNull()
    await click(copyButton())
    expect(asked).toHaveBeenCalledTimes(1)
    expect(written).toEqual([TEXT])
    expect(copyButton().textContent).toBe('Copied')
    expect(host.textContent).toContain('no key, no proxy address, no target and no message')
    expect(host.querySelector('textarea')).toBeNull()
  })

  it('shows the diagnostics to copy by hand when the browser refuses to copy', async () => {
    clipboardThat(async () => { throw new DOMException('Write permission denied.', 'NotAllowedError') })
    vi.spyOn(api, 'systemDiagnostics').mockResolvedValue({ text: TEXT })
    await openCheck({ ready: true, items: [MODEL] })
    await click(copyButton())
    const shown = host.querySelector('textarea')!
    expect(shown.value).toBe(TEXT)
    expect(shown.readOnly).toBe(true)
    expect(host.textContent).toContain('select the text below and copy it')
    expect(copyButton().textContent).toBe('Copy diagnostics')
  })

  it('says why when the diagnostics cannot be had', async () => {
    clipboardThat(async () => {})
    vi.spyOn(api, 'systemDiagnostics').mockRejectedValue(new Error('404: Not Found'))
    await openCheck({ ready: true, items: [MODEL] })
    await click(copyButton())
    expect(alerts()).toEqual(['The diagnostics could not be had: 404: Not Found'])
    expect(copyButton().textContent).toBe('Copy diagnostics')
  })
})
