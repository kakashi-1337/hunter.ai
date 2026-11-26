// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { SettingsDialog } from '../components/SettingsDialog'
import { useStore } from '../store'
import type { LocalModel, Settings } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/** What the server sends for a new install. */
const SETTINGS: Settings = {
  default_provider: 'abliteration', default_model: 'big', fallback_model: 'qwen3:8b', local_window: 16384, local_window_max: 131072,
  keys: { ABLIT_API_KEY: true }, permissions: {}, max_turns: 60, swarm_width: 3, swarm_width_max: 6,
  machine: { cores: 8, memory_gb: 32 }, presets: {}, decision_provider: null, decision_model: null, decision_gate: false,
  decision_engine: true, kb_redact: true, kb_skip_generated: true, vision_model: null, browser_via_caido: false, answer_check: false,
  mcp_on_demand: true, notify_findings: false, notify_min_severity: 'high', notify_webhook_set: false, caido_available: false,
  upstream_proxy: '', upstream_proxy_password: false, upstream_proxy_intercepts: false, upstream_proxy_kb: false,
  provider_proxy: '', provider_proxy_password: false, colibri_url: '', colibri_key_set: false, interface_font: '',
}

/** What the server says about the fallback model of this machine. */
const LOCAL: LocalModel = {
  answers: true, model: 'qwen3:8b', installed: true, window: 16384, limit: 40960, bytes_per_token: 147456,
  overhead: { tokens: 6752, tools: 36, on_demand: 0 }, enough: 16384, loaded: null,
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
  vi.spyOn(api, 'systemLocal').mockResolvedValue(LOCAL)
  vi.spyOn(api, 'systemLlamaCppFind').mockRejectedValue(new Error('this test does not look at the machine'))
  put = vi.spyOn(api, 'putSettings').mockImplementation(async (patch) => {
    const sent = patch as { local_window?: number | null; local_window_max?: number | null }
    return { ...SETTINGS, ...patch, local_window: sent.local_window ?? 16384, local_window_max: sent.local_window_max ?? 131072 } as Settings
  })
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
const largestField = () => field(/^Largest context window for local models/).querySelector<HTMLInputElement>('input')!

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

  describe('the interface font', () => {
    const fontField = () => field(/^Interface font/).querySelector<HTMLInputElement>('input')!
    const said = () => field(/^Interface font/).querySelector('[role="alert"]')?.textContent ?? null

    it('is empty for the font of the system, and says what a name is', async () => {
      await open()
      expect(fontField().value).toBe('')
      expect(field(/^Interface font/).textContent).toContain('a font that is installed on this computer, like Ubuntu Mono')
      expect(said()).toBeNull()
    })

    it('names the font the owner chose', async () => {
      useStore.setState({ settings: { ...SETTINGS, interface_font: 'Ubuntu Mono' } })
      vi.spyOn(api, 'getSettings').mockResolvedValue({ ...SETTINGS, interface_font: 'Ubuntu Mono' })
      await open()
      expect(fontField().value).toBe('Ubuntu Mono')
    })

    it('offers the fonts of Ubuntu and takes any other name', async () => {
      await open()
      const offered = [...host.querySelectorAll(`datalist#${fontField().getAttribute('list')} option`)].map((o) => o.getAttribute('value'))
      expect(offered.slice(0, 3)).toEqual(['Ubuntu Mono', 'Ubuntu Sans Mono', 'Ubuntu Sans'])
    })

    it('saves the name when the field is left, without the spaces around it', async () => {
      await open()
      await leaveWith(fontField(), '  Ubuntu Mono ')
      expect(put).toHaveBeenCalledWith({ interface_font: 'Ubuntu Mono' })
      expect(fontField().value).toBe('Ubuntu Mono')
    })

    it('goes back to the font of the system when the field is emptied', async () => {
      useStore.setState({ settings: { ...SETTINGS, interface_font: 'Ubuntu Mono' } })
      vi.spyOn(api, 'getSettings').mockResolvedValue({ ...SETTINGS, interface_font: 'Ubuntu Mono' })
      await open()
      await leaveWith(fontField(), '')
      expect(put).toHaveBeenCalledWith({ interface_font: '' })
    })

    it('asks the server nothing when the name did not change', async () => {
      useStore.setState({ settings: { ...SETTINGS, interface_font: 'Ubuntu Mono' } })
      vi.spyOn(api, 'getSettings').mockResolvedValue({ ...SETTINGS, interface_font: 'Ubuntu Mono' })
      await open()
      await leaveWith(fontField(), 'Ubuntu Mono ')
      expect(put).not.toHaveBeenCalled()
    })

    it('does not send what is more than a name, and says how a font is named', async () => {
      await open()
      await leaveWith(fontField(), "x', serif; } body { display: none")
      expect(put).not.toHaveBeenCalled()
      expect(said()).toBe('A font is named with letters, digits, spaces and . _ + - (60 characters at most), like Ubuntu Mono.')
    })
  })

  describe('the context window for local models', () => {
    it('shows the size in use and says what it is for', async () => {
      await open()
      expect(windowField().value).toBe('16384')
      expect(field(/^Context window for local models/).textContent).toMatch(/summary of its early part/)
    })

    it('says what a window costs for the fallback model of this machine and what the rules and tools take', async () => {
      await open()
      const said = field(/^Context window for local models/).textContent ?? ''
      expect(said).toContain('With qwen3:8b, each 8,192 tokens of window take about 1.1 GB of memory beside the model itself. It takes 40,960 tokens at most.')
      expect(said).toContain("HunterAI's rules and tools take about 6,752 tokens of the window (36 tools).")
      expect(field(/^Context window for local models/).querySelector('[role="alert"]')).toBeNull()
    })

    it('warns when the window in use leaves no room for a chat beside the rules and tools, and stays as it is', async () => {
      useStore.setState({ settings: { ...SETTINGS, local_window_max: 0 } })
      vi.spyOn(api, 'getSettings').mockResolvedValue({ ...SETTINGS, local_window_max: 0 })
      vi.spyOn(api, 'systemLocal').mockResolvedValue({ ...LOCAL, overhead: { tokens: 25608, tools: 112, on_demand: 40 }, enough: 36864 })
      await open()
      expect(field(/^Context window for local models/).querySelector('[role="alert"]')?.textContent)
        .toBe('Too small here: with the rules and tools, less than 8,192 tokens are left for the chat. Set 36,864 or more.')
    })

    it('does not warn about small room when a chat\'s window grows by itself: it says what that costs', async () => {
      vi.spyOn(api, 'systemLocal').mockResolvedValue({ ...LOCAL, limit: 131072, overhead: { tokens: 25608, tools: 112, on_demand: 40 }, enough: 36864 })
      await open()
      expect(field(/^Context window for local models/).querySelector('[role="alert"]')).toBeNull()
      expect(field(/^Context window for local models/).textContent).toContain('the window of every chat grows by itself at its start')
    })

    it('says that a chat which needs more room gets it by itself, before anything of it is left out', async () => {
      await open()
      expect(field(/^Context window for local models/).textContent).toMatch(/has its window raised by itself, up to the largest below/)
    })

    it('still shows the field when the server cannot describe the model', async () => {
      vi.spyOn(api, 'systemLocal').mockRejectedValue(new Error('404: Not Found'))
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

    it('shows how far a window grows by itself, and says what growing means', async () => {
      await open()
      expect(largestField().value).toBe('131072')
      const said = field(/^Largest context window for local models/).textContent ?? ''
      expect(said).toMatch(/does not fit its window, or an answer fills it/)
      expect(said).toMatch(/0: a window stays as it is set/)
    })

    it('saves another largest size, 0 for a window that stays as it is, and nothing for the size HunterAI uses by itself', async () => {
      await open()
      await leaveWith(largestField(), '65536')
      expect(put).toHaveBeenLastCalledWith({ local_window_max: 65536 })
      await leaveWith(largestField(), '0')
      expect(put).toHaveBeenLastCalledWith({ local_window_max: 0 })
      await leaveWith(largestField(), '')
      expect(put).toHaveBeenLastCalledWith({ local_window_max: null })
      expect(largestField().value).toBe('131072')
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

describe('the cloud model for the subagents of a local chat', () => {
  const model = (id: string) => ({ id, label: id, supports_images: false, options: [], context_window: null })
  const PROVIDERS = [
    { name: 'abliteration', configured: true, models: [model('big'), model('small')] },
    { name: 'anthropic', configured: false, models: [] },
    { name: 'ollama', configured: true, models: [model('qwen3:8b')] },
    { name: 'codex', configured: true, models: [model('codex-default')] },
  ]
  const KNOWS: Settings = { ...SETTINGS, subagent_provider: null, subagent_model: null, subagent_providers: ['abliteration', 'anthropic'] }
  const provider = () => field(/^Cloud model for the subagents of a local chat/)?.querySelector<HTMLSelectElement>('select')
  const choose = async (select: HTMLSelectElement, value: string) => {
    await act(async () => { select.value = value; select.dispatchEvent(new Event('change', { bubbles: true })) })
  }
  /** The dialog asks the server for the settings when it opens: that answer and the store hold the same. */
  const serverSends = (settings: Settings) => {
    useStore.setState({ settings, providers: PROVIDERS })
    vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
    put.mockImplementation(async (patch: Partial<Settings>) => ({ ...settings, ...patch }) as Settings)
  }

  it('is none at the start, and offers the cloud providers the server names that are set up', async () => {
    serverSends(KNOWS)
    await open()
    expect([...provider()!.options].map((o) => [o.value, o.textContent])).toEqual([['', 'None: a local model works alone'], ['abliteration', 'abliteration']])
    expect(provider()!.value).toBe('')
    expect(host.textContent).toContain('goes to that provider')
  })

  it('saves a provider with its first model, and clears both together', async () => {
    serverSends(KNOWS)
    await open()
    await choose(provider()!, 'abliteration')
    expect(put).toHaveBeenLastCalledWith({ subagent_provider: 'abliteration', subagent_model: 'big' })
    await choose(provider()!, '')
    expect(put).toHaveBeenLastCalledWith({ subagent_provider: '', subagent_model: '' })
  })

  it('shows a choice whose provider has lost its key, and says that the local model works alone meanwhile', async () => {
    serverSends({ ...KNOWS, subagent_provider: 'anthropic', subagent_model: 'claude-x' })
    await open()
    expect(provider()!.value).toBe('anthropic')
    expect(host.textContent).toContain('anthropic has no key now')
  })

  it('is left out for a server from before it knew of it', async () => {
    serverSends(SETTINGS)
    await open()
    expect(provider()).toBeUndefined()
  })
})

describe('where the embedding model runs', () => {
  const box = () => [...host.querySelectorAll('label.check')].find((l) => /embedding model on the processor/.test(l.textContent ?? ''))?.querySelector<HTMLInputElement>('input')

  it('is a switch that is off at the start, and is saved when it is set', async () => {
    const knows: Settings = { ...SETTINGS, embed_on_cpu: false }
    useStore.setState({ settings: knows, providers: [] })
    vi.spyOn(api, 'getSettings').mockResolvedValue(knows)
    await open()
    expect(box()!.checked).toBe(false)
    await act(async () => { box()!.click() })
    expect(put).toHaveBeenLastCalledWith({ embed_on_cpu: true })
  })

  it('is left out for a server from before it knew of it', async () => {
    await open()
    expect(box()).toBeUndefined()
  })
})

describe('leaving out what nobody wrote when a folder is indexed', () => {
  const label = () => [...host.querySelectorAll('label.check')].find((l) => /^Leave out what nobody wrote/.test(l.textContent ?? ''))
  const box = () => label()?.querySelector<HTMLInputElement>('input')

  it('is a switch that is on at the start, and is saved when it is turned off', async () => {
    await open()
    expect(box()!.checked).toBe(true)
    await act(async () => { box()!.click() })
    expect(put).toHaveBeenLastCalledWith({ kb_skip_generated: false })
  })

  it('says what is left out, and how a collection that was filled before is cleaned', async () => {
    await open()
    const text = label()!.textContent ?? ''
    for (const said of ['.next', 'minified', 'source maps', 'lock files', 'skipped', 'scripts/kb-clean.sh']) expect(text).toContain(said)
  })
})

describe("searching a chat's collections before every answer", () => {
  const label = () => [...host.querySelectorAll('label.check')].find((l) => /^Search a chat's collections before every answer/.test(l.textContent ?? ''))
  const box = () => label()?.querySelector<HTMLInputElement>('input')

  it('is a switch that is on at the start, also for a server from before it knew of it, and is saved when it is turned off', async () => {
    await open()
    expect(box()!.checked).toBe(true)           // SETTINGS above has no kb_lookup: that is such a server
    await act(async () => { box()!.click() })
    expect(put).toHaveBeenLastCalledWith({ kb_lookup: false })
  })

  it('shows what the server says, and is saved when it is turned on', async () => {
    useStore.setState({ settings: { ...SETTINGS, kb_lookup: false } })
    vi.spyOn(api, 'getSettings').mockResolvedValue({ ...SETTINGS, kb_lookup: false })
    await open()
    expect(box()!.checked).toBe(false)
    await act(async () => { box()!.click() })
    expect(put).toHaveBeenLastCalledWith({ kb_lookup: true })
  })

  it('says where a chat decides for itself, what a search costs, and how a chat gets its collections', async () => {
    await open()
    const text = label()!.textContent ?? ''
    for (const said of ['"Always retrieve"', 'one request to the embedding model', '6,000 characters', '"+ Knowledge"', '"every chat"']) expect(text).toContain(said)
  })
})

describe('asking the web before every answer', () => {
  const label = () => [...host.querySelectorAll('label.check')].find((l) => /^Ask the web before every answer/.test(l.textContent ?? ''))
  const box = () => label()?.querySelector<HTMLInputElement>('input')

  it('is a switch that is off at the start, also for a server from before it knew of it, and is saved when it is turned on', async () => {
    await open()
    expect(box()!.checked).toBe(false)
    await act(async () => { box()!.click() })
    expect(put).toHaveBeenLastCalledWith({ web_lookup: true })
  })

  it('shows what the server says, and is saved when it is turned off', async () => {
    useStore.setState({ settings: { ...SETTINGS, web_lookup: true } })
    vi.spyOn(api, 'getSettings').mockResolvedValue({ ...SETTINGS, web_lookup: true })
    await open()
    expect(box()!.checked).toBe(true)
    await act(async () => { box()!.click() })
    expect(put).toHaveBeenLastCalledWith({ web_lookup: false })
  })

  it('says where the words go, what is left out of them, and where the search is shown', async () => {
    await open()
    const text = label()!.textContent ?? ''
    for (const said of ['your own SearXNG', 'public search engines', 'host names', 'mail addresses', 'a password', 'shown under the answer', 'web_search is denied']) expect(text).toContain(said)
  })
})
