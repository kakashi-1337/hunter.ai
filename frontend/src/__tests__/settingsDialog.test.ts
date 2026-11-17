// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { SettingsDialog } from '../components/SettingsDialog'
import { useStore } from '../store'
import type { Settings } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/** What the server sends for a new install. */
const SETTINGS: Settings = {
  default_provider: 'abliteration', default_model: 'big', fallback_model: 'qwen3:8b', local_window: 16384,
  keys: { ABLIT_API_KEY: true }, permissions: {}, max_turns: 60, swarm_width: 3, swarm_width_max: 6,
  machine: { cores: 8, memory_gb: 32 }, presets: {}, decision_provider: null, decision_model: null, decision_gate: false,
  decision_engine: true, kb_redact: true, vision_model: null, browser_via_caido: false, answer_check: false,
  mcp_on_demand: true, notify_findings: false, notify_min_severity: 'high', notify_webhook_set: false, caido_available: false,
  upstream_proxy: '', upstream_proxy_password: false, upstream_proxy_intercepts: false, upstream_proxy_kb: false,
  provider_proxy: '', provider_proxy_password: false,
}

let root: Root
let host: HTMLElement
let put: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  useStore.setState({ settings: SETTINGS, providers: [], error: null })
  vi.spyOn(api, 'getSettings').mockResolvedValue(SETTINGS)
  vi.spyOn(api, 'listTools').mockResolvedValue([])
  vi.spyOn(api, 'mcpStatus').mockResolvedValue({ configured: [], servers: [], error: null })
  put = vi.spyOn(api, 'putSettings').mockImplementation(async (patch) => ({ ...SETTINGS, ...patch, local_window: (patch as { local_window?: number | null }).local_window ?? 16384 }) as Settings)
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.restoreAllMocks()
})

async function open(): Promise<void> {
  await act(async () => { root.render(createElement(SettingsDialog, { onClose: () => {} })) })
}
const field = (label: RegExp) => [...host.querySelectorAll('label.field')].find((l) => label.test(l.querySelector('span')?.textContent ?? ''))!
const windowField = () => field(/^Context window for local models/).querySelector<HTMLInputElement>('input')!

async function leaveWith(el: HTMLInputElement, value: string): Promise<void> {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  await act(async () => {
    set.call(el, value)
    el.dispatchEvent(new Event('input', { bubbles: true }))
    el.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
  })
}

describe('Settings', () => {
  it('opens with what the server sends for a new install', async () => {
    await open()
    expect(host.querySelector('[role="dialog"]')?.getAttribute('aria-label')).toBe('Settings')
    expect(host.querySelector('[role="alert"]')).toBeNull()
    expect(host.textContent).toContain('Fallback model (Ollama)')
  })

  describe('the context window for local models', () => {
    it('shows the size in use and says what it is for', async () => {
      await open()
      expect(windowField().value).toBe('16384')
      expect(field(/^Context window for local models/).textContent).toMatch(/summary of its early part/)
    })

    it('is saved when the field is left with another size', async () => {
      await open()
      await leaveWith(windowField(), '32768')
      expect(put).toHaveBeenCalledWith({ local_window: 32768 })
      expect(windowField().value).toBe('32768')
    })

    it('goes back to the size HunterAI uses by itself when the field is emptied', async () => {
      useStore.setState({ settings: { ...SETTINGS, local_window: 32768 } })
      vi.spyOn(api, 'getSettings').mockResolvedValue({ ...SETTINGS, local_window: 32768 })
      await open()
      await leaveWith(windowField(), '')
      expect(put).toHaveBeenCalledWith({ local_window: null })
      expect(windowField().value).toBe('16384')
    })

    it('is not saved when the field is left as it was', async () => {
      await open()
      await leaveWith(windowField(), '16384')
      expect(put).not.toHaveBeenCalled()
    })

    it('says why the server refused a size, and shows the size that is still in use', async () => {
      put.mockRejectedValue(new Error('The context window for local models must be 2,048 to 1,048,576 tokens'))
      await open()
      await leaveWith(windowField(), '100')
      expect(useStore.getState().error).toContain('2,048 to 1,048,576')
      expect(windowField().value).toBe('16384')
    })
  })
})
