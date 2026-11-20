// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import * as build from '../build'
import { MachineStatus } from '../components/MachineStatus'
import { reloadNotice, renderItems } from '../tree'
import type { Block, Message, SystemStatus } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const CUT = 'The answer was cut off at the limit the model has for one answer. Send "continue" to have it go on.'
const message = (blocks: Block[]): Message => ({
  id: 'm1', chat_id: 'c1', parent_id: null, role: 'assistant', blocks, provider: 'ollama', model: 'qwen3:8b',
  fallback_from: null, status: 'complete', created_at: '2026-11-17T10:00:00Z', siblings: [],
})

describe('the word under an answer that did not end by itself', () => {
  it('is drawn after the text it is about', () => {
    expect(renderItems(message([{ type: 'text', text: 'The first step is to' }, { type: 'notice', kind: 'cut_off', text: CUT }])))
      .toEqual([{ kind: 'text', text: 'The first step is to' }, { kind: 'notice', text: CUT, level: 'warn' }])
  })

  it('is left out when it says nothing', () => {
    expect(renderItems(message([{ type: 'text', text: 'done' }, { type: 'notice', kind: 'cut_off', text: '' }]))).toEqual([{ kind: 'text', text: 'done' }])
  })

  it('does not break the page when a newer server sends a kind of block this page does not know', () => {
    const future = { type: 'verdict', score: 3 } as unknown as Block
    expect(renderItems(message([{ type: 'text', text: 'done' }, future]))).toEqual([{ kind: 'text', text: 'done' }])
  })
})

const QUIET: SystemStatus = { cpu_temp: null, cpu_limit: 90, local_models: [], restart_needed: false, stopped: [], page: '/assets/index-NEW.js' }

describe('the notice that the page is older than the server', () => {
  it('is given when the interface was built again after this page was opened', () => {
    expect(reloadNotice(QUIET, '/assets/index-OLD.js')).toBe('Reload this page: HunterAI was updated after it was opened, and it still shows the interface from before.')
  })

  it('is not given while the page runs the interface that is built now', () => {
    expect(reloadNotice(QUIET, '/assets/index-NEW.js')).toBeNull()
  })

  it('is not given when either side cannot tell: no status, an older server, no build, a page served for development', () => {
    expect(reloadNotice(null, '/assets/index-OLD.js')).toBeNull()
    const { page: _, ...old } = QUIET
    expect(reloadNotice(old as SystemStatus, '/assets/index-OLD.js')).toBeNull()
    expect(reloadNotice({ ...QUIET, page: null }, '/assets/index-OLD.js')).toBeNull()
    expect(reloadNotice(QUIET, null)).toBeNull()
    expect(reloadNotice(QUIET, '/src/main.tsx')).toBeNull()
  })
})

describe('the page that is older than the server', () => {
  let root: Root
  let host: HTMLElement
  beforeEach(() => {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
  })
  afterEach(() => {
    act(() => root.unmount())
    host.remove()
    vi.restoreAllMocks()
  })
  const alerts = () => [...host.querySelectorAll('[role="alert"]')].map((a) => a.textContent ?? '')

  it('says so above the machine line, with a button that reloads it', async () => {
    vi.spyOn(api, 'systemStatus').mockResolvedValue(QUIET)
    vi.spyOn(build, 'ownScript').mockReturnValue('/assets/index-OLD.js')
    const reload = vi.spyOn(build, 'reload').mockImplementation(() => {})
    await act(async () => { root.render(createElement(MachineStatus)) })
    expect(alerts()).toHaveLength(1)
    expect(alerts()[0]).toContain('Reload this page: HunterAI was updated after it was opened')
    const button = [...host.querySelectorAll('button')].find((b) => b.textContent === 'Reload')!
    await act(async () => { button.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('says nothing while it runs the interface that is built now', async () => {
    vi.spyOn(api, 'systemStatus').mockResolvedValue(QUIET)
    vi.spyOn(build, 'ownScript').mockReturnValue('/assets/index-NEW.js')
    await act(async () => { root.render(createElement(MachineStatus)) })
    expect(alerts()).toEqual([])
  })
})

describe('the script this page runs', () => {
  afterEach(() => { document.head.querySelectorAll('script[data-test]').forEach((s) => s.remove()) })

  it('is the one its own file names', () => {
    const tag = document.createElement('script')
    tag.setAttribute('type', 'module'); tag.setAttribute('data-test', ''); tag.setAttribute('src', '/assets/index-CXRQE8sX.js')
    document.head.appendChild(tag)
    expect(build.ownScript()).toBe('/assets/index-CXRQE8sX.js')
  })

  it('is unknown on a page that was not built', () => {
    expect(build.ownScript()).toBeNull()
  })
})
