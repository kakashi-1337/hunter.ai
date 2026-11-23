// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { ModelPicker } from '../components/ModelPicker'
import { SettingsDialog } from '../components/SettingsDialog'
import { useStore } from '../store'
import { colibriHint, providerGroup } from '../tree'
import type { Chat, ColibriServer, LocalModel, ProviderInfo, Settings } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const AT = 'http://127.0.0.1:8000'
const OFF: ColibriServer = { url: '', state: 'off', said: '', model: null, context: null, images: false, overhead: null, enough: null }
const SERVES: ColibriServer = { url: AT, state: 'ok', said: '', model: 'qwen3.6-colibri', context: 65536, images: true, overhead: 25608, enough: null }
const DOWN: ColibriServer = { ...OFF, url: AT, state: 'down', said: `nothing answers at ${AT}` }
const MODEL = { id: 'qwen3.6-colibri', label: 'qwen3.6-colibri', supports_images: true, options: ['temperature', 'thinking'], context_window: 65536, efforts: [] }

describe('what Settings says under the address of colibri', () => {
  it('says nothing while no address is set or the server was not asked', () => {
    expect(colibriHint(null, false)).toEqual({ lines: [], warn: null })
    expect(colibriHint(OFF, false)).toEqual({ lines: [], warn: null })
  })

  it('names the model it serves, its context and what it reads, and what the rules and tools take of it', () => {
    expect(colibriHint(SERVES, false)).toEqual({
      lines: ['It serves qwen3.6-colibri: a context of 65,536 tokens, reads pictures. Pick it in the model list of a chat.',
        "HunterAI's rules and tools take about 25,608 tokens of that context."],
      warn: null,
    })
    expect(colibriHint({ ...SERVES, images: false, context: null, overhead: null }, false).lines)
      .toEqual(['It serves qwen3.6-colibri: a context it does not name, reads text only. Pick it in the model list of a chat.'])
  })

  it('warns when its context leaves no room for a chat, with what to start it with', () => {
    expect(colibriHint({ ...SERVES, context: 32768, enough: 65536 }, false).warn).toBe(
      'Too small here: with the rules and tools, less than 8,192 tokens are left for the chat. Start colibri with a larger context: '
      + './start-here.sh --ctx 65536 in the colibri folder (a larger context needs more memory).')
  })

  it('says how to start a colibri that does not answer, without alarm: it may be off on purpose', () => {
    expect(colibriHint(DOWN, false)).toEqual({
      lines: [`Nothing answers at ${AT}. Start colibri (./start-here.sh in the colibri folder), then press Check.`], warn: null })
  })

  it('says which key is wanted, by whether one is set', () => {
    const wants: ColibriServer = { ...OFF, url: AT, state: 'key', said: 'it asks for a key: enter it under Settings' }
    expect(colibriHint(wants, false).warn).toBe(`colibri at ${AT} asks for a key: enter the one it was started with below.`)
    expect(colibriHint(wants, true).warn).toBe(`colibri at ${AT} did not accept the key that is set: enter the one it was started with below.`)
  })

  it('passes on what else stands in the way, in the words of the server', () => {
    const refused: ColibriServer = { ...OFF, url: AT, state: 'refused', said: "it refused the request: Host header 'box.lan' not allowed. Add it with --allowed-host box.lan" }
    expect(colibriHint(refused, false)).toEqual({ lines: [], warn: "colibri: it refused the request: Host header 'box.lan' not allowed. Add it with --allowed-host box.lan." })
    expect(colibriHint({ ...OFF, url: AT, state: 'odd', said: `what answers at ${AT} is not colibri (it lists no model)` }, false).warn)
      .toBe(`colibri: what answers at ${AT} is not colibri (it lists no model).`)
  })
})

describe('colibri in the list of models', () => {
  const entry = (more: Partial<ProviderInfo>): ProviderInfo => ({ name: 'colibri', configured: true, models: [], ...more })

  it('says where to set it up while it has no address', () => {
    expect(providerGroup(entry({ configured: false }))).toEqual({ label: 'colibri — set its address in Settings', empty: 'not configured' })
  })

  it('says why it offers no model', () => {
    expect(providerGroup(entry({ note: `nothing answers at ${AT}` }))).toEqual({ label: 'colibri', empty: `nothing answers at ${AT}` })
  })

  it('has nothing to say when it serves one', () => {
    expect(providerGroup(entry({ models: [MODEL] }))).toEqual({ label: 'colibri', empty: null })
  })

  it('leaves the other providers as they were', () => {
    expect(providerGroup({ name: 'openai', configured: false, models: [] })).toEqual({ label: 'openai — add key in settings', empty: 'not configured' })
    expect(providerGroup({ name: 'codex', configured: false, models: [] }).label).toBe('codex — install the codex CLI')
    expect(providerGroup({ name: 'ollama', configured: true, models: [] })).toEqual({ label: 'ollama', empty: 'no models available' })
  })
})

const SETTINGS: Settings = {
  default_provider: 'abliteration', default_model: 'big', fallback_model: 'qwen3:8b', local_window: 16384, local_window_max: 131072,
  keys: { ABLIT_API_KEY: true }, permissions: {}, max_turns: 60, swarm_width: 3, swarm_width_max: 6,
  machine: { cores: 8, memory_gb: 32 }, presets: {}, decision_provider: null, decision_model: null, decision_gate: false,
  decision_engine: true, kb_redact: true, kb_skip_generated: true, vision_model: null, browser_via_caido: false, answer_check: false,
  mcp_on_demand: true, notify_findings: false, notify_min_severity: 'high', notify_webhook_set: false, caido_available: false,
  upstream_proxy: '', upstream_proxy_password: false, upstream_proxy_intercepts: false, upstream_proxy_kb: false,
  provider_proxy: '', provider_proxy_password: false, colibri_url: '', colibri_key_set: false,
}
const LOCAL: LocalModel = {
  answers: true, model: 'qwen3:8b', installed: true, window: 16384, limit: 40960, bytes_per_token: 147456,
  overhead: { tokens: 6752, tools: 36, on_demand: 0 }, enough: 16384, loaded: null,
}

let root: Root
let host: HTMLElement
let put: ReturnType<typeof vi.spyOn>
let look: ReturnType<typeof vi.spyOn>
let listed: ReturnType<typeof vi.spyOn>

function install(settings: Settings, server: ColibriServer): void {
  useStore.setState({ settings, providers: [], error: null })
  vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
  look = vi.spyOn(api, 'systemColibri').mockResolvedValue(server)
}

beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  vi.spyOn(api, 'listTools').mockResolvedValue([])
  vi.spyOn(api, 'mcpStatus').mockResolvedValue({ configured: [], servers: [], error: null })
  vi.spyOn(api, 'systemLocal').mockResolvedValue(LOCAL)
  listed = vi.spyOn(api, 'listProviders').mockResolvedValue([{ name: 'colibri', configured: true, models: [MODEL] }])
  put = vi.spyOn(api, 'putSettings').mockImplementation(async (patch) => ({ ...SETTINGS, ...patch, colibri_url: AT }) as Settings)
  install(SETTINGS, OFF)
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.restoreAllMocks()
})

async function open(): Promise<void> {
  await act(async () => { root.render(createElement(SettingsDialog, { onClose: () => {} })) })
}
const address = () => host.querySelector<HTMLInputElement>('input[aria-label="colibri address"]')!
const section = () => address().closest('.colibri') as HTMLElement
const button = (name: string) => [...section().querySelectorAll('button')].find((b) => b.textContent === name)!
const keyRow = () => section().querySelector('input[type="password"]')!.closest('.keyrow') as HTMLElement

async function type(el: HTMLInputElement, value: string): Promise<void> {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  await act(async () => {
    set.call(el, value)
    el.dispatchEvent(new Event('input', { bubbles: true }))
  })
}
const press = (el: HTMLElement) => act(async () => { el.click() })

describe('colibri in Settings', () => {
  it('is not used and not asked until the owner gives its address', async () => {
    await open()
    expect(address().value).toBe('')
    expect(address().placeholder).toBe(AT)
    expect(section().textContent).toContain('none')
    expect(look).not.toHaveBeenCalled()
    expect(button('Clear').disabled).toBe(true)
    expect(button('Check').disabled).toBe(true)
  })

  it('saves an address, shows it as it is reached, and says what answers there', async () => {
    look.mockResolvedValue(SERVES)
    await open()
    await type(address(), ' 127.0.0.1:8000/v1 ')
    await press(button('Save'))
    expect(put).toHaveBeenCalledWith({ colibri_url: ' 127.0.0.1:8000/v1 ' })
    expect(address().value).toBe(AT)
    expect(section().textContent).toContain('in use')
    expect(section().textContent).toContain('It serves qwen3.6-colibri: a context of 65,536 tokens, reads pictures.')
    expect(section().querySelector('[role="alert"]')).toBeNull()
    expect(listed).toHaveBeenCalled()                     // the model list of the chats has it at once
    expect(useStore.getState().providers[0].models[0].id).toBe('qwen3.6-colibri')
  })

  it('says why the server refused an address, and keeps what was typed', async () => {
    put.mockRejectedValue(new Error('colibri address: it has to begin with http:// or https://'))
    await open()
    await type(address(), 'ftp://127.0.0.1:8000')
    await press(button('Save'))
    expect(section().querySelector('[role="alert"]')?.textContent).toBe('colibri address: it has to begin with http:// or https://')
    expect(address().value).toBe('ftp://127.0.0.1:8000')
    expect(look).not.toHaveBeenCalled()
  })

  it('asks the server when Settings opens with an address, and again on Check', async () => {
    install({ ...SETTINGS, colibri_url: AT }, DOWN)
    await open()
    expect(look).toHaveBeenCalledTimes(1)
    expect(section().textContent).toContain(`Nothing answers at ${AT}. Start colibri (./start-here.sh in the colibri folder), then press Check.`)
    expect(section().querySelector('[role="alert"]')).toBeNull()
    look.mockResolvedValue(SERVES)                        // the owner started it
    listed.mockClear()
    await press(button('Check'))
    expect(look).toHaveBeenCalledTimes(2)
    expect(section().textContent).toContain('It serves qwen3.6-colibri')
    expect(section().textContent).not.toContain('Nothing answers')
    expect(listed).toHaveBeenCalledTimes(1)
  })

  it('stops saying what answers at the saved address while another one is being typed', async () => {
    install({ ...SETTINGS, colibri_url: AT }, { ...SERVES, context: 32768, enough: 65536 })
    await open()
    expect(section().textContent).toContain('It serves qwen3.6-colibri')
    expect(button('Check').disabled).toBe(false)
    await type(address(), 'http://127.0.0.1:9000')
    expect(section().textContent).not.toContain('It serves qwen3.6-colibri')
    expect(section().querySelector('[role="alert"]')).toBeNull()
    expect(button('Check').disabled).toBe(true)           // Check asks the saved address, and that is not the one in the field
    expect(button('Save').disabled).toBe(false)
  })

  it('warns when the context of the model leaves no room for a chat', async () => {
    install({ ...SETTINGS, colibri_url: AT }, { ...SERVES, context: 32768, enough: 65536 })
    await open()
    expect(section().querySelector('[role="alert"]')?.textContent).toContain('./start-here.sh --ctx 65536 in the colibri folder')
  })

  it('clears the address when the field is emptied', async () => {
    install({ ...SETTINGS, colibri_url: AT }, SERVES)
    put.mockImplementation(async (patch: object) => ({ ...SETTINGS, ...patch, colibri_url: '' }) as Settings)
    await open()
    listed.mockClear()
    await type(address(), '')
    await press(button('Clear'))
    expect(put).toHaveBeenCalledWith({ colibri_url: '' })
    expect(address().value).toBe('')
    expect(listed).toHaveBeenCalledTimes(1)               // the model list of the chats no longer offers what it served
    expect(section().textContent).not.toContain('It serves qwen3.6-colibri')
    expect(section().textContent).toContain('none')
  })

  it('takes the key colibri was started with, beside the address and not among the cloud keys', async () => {
    const key = vi.spyOn(api, 'putKey').mockResolvedValue(undefined)
    install({ ...SETTINGS, colibri_url: AT }, { ...OFF, url: AT, state: 'key', said: 'it asks for a key: enter it under Settings' })
    await open()
    expect(section().querySelector('[role="alert"]')?.textContent).toBe(`colibri at ${AT} asks for a key: enter the one it was started with below.`)
    const field = section().querySelector<HTMLInputElement>('input[type="password"]')!
    expect(field).not.toBeNull()
    expect(keyRow().textContent).toContain('colibri key, only when it was started with one COLIBRI_API_KEY · not set')
    vi.spyOn(api, 'getSettings').mockResolvedValue({ ...SETTINGS, colibri_url: AT, colibri_key_set: true })
    look.mockResolvedValue(SERVES)
    await type(field, 'secret-key-1')
    await press([...section().querySelectorAll('button')].filter((b) => b.textContent === 'Save').pop()!)
    expect(key).toHaveBeenCalledWith('COLIBRI_API_KEY', 'secret-key-1')
    expect(section().textContent).toContain('It serves qwen3.6-colibri')
    expect(section().querySelector('[role="alert"]')).toBeNull()
    expect(field.value).toBe('')                          // a key is never left on the page
    expect(keyRow().textContent).toContain('colibri key, only when it was started with one COLIBRI_API_KEY · set')
  })

  it('says that the key which is set was not accepted, and not that one is wanted', async () => {
    install({ ...SETTINGS, colibri_url: AT, colibri_key_set: true }, { ...OFF, url: AT, state: 'key', said: 'it did not accept the key that is set under Settings' })
    await open()
    expect(section().querySelector('[role="alert"]')?.textContent).toBe(`colibri at ${AT} did not accept the key that is set: enter the one it was started with below.`)
    expect(keyRow().textContent).toContain('· set')
  })

  it('shows an address the server keeps in another form than it was typed in, though it is the one already saved', async () => {
    install({ ...SETTINGS, colibri_url: AT }, SERVES)
    await open()
    await type(address(), '127.0.0.1:8000/v1')            // the same server, as colibri prints its address
    await press(button('Save'))
    expect(put).toHaveBeenCalledWith({ colibri_url: '127.0.0.1:8000/v1' })
    expect(address().value).toBe(AT)
    expect(section().textContent).toContain('It serves qwen3.6-colibri')
  })

  it('still shows the field when the server cannot be asked what answers there', async () => {
    install({ ...SETTINGS, colibri_url: AT }, SERVES)
    look.mockRejectedValue(new Error('404: Not Found'))   // a server from before it could say
    await open()
    expect(address().value).toBe(AT)
    expect(section().querySelector('[role="alert"]')).toBeNull()
  })
})

describe('Settings on a server from before it knew colibri', () => {
  /** What such a server sends: everything but the two fields this page added. The owner updated, the page is the
   * new one, and the server that answers was not started again yet. */
  const before = (): Settings => {
    const { colibri_url: _url, colibri_key_set: _key, ...older } = SETTINGS
    void _url; void _key
    return older as Settings
  }

  it('opens all the same, and every other setting is there', async () => {
    install(before(), OFF)
    await open()
    expect(host.querySelector('[role="dialog"]')?.getAttribute('aria-label')).toBe('Settings')
    expect(host.textContent).not.toContain('This part of the page could not be shown')
    expect(host.textContent).toContain('Fallback model (Ollama)')
    expect(host.textContent).toContain('Max model turns per run')
  })

  it('says under colibri that the server has to be started again, and offers nothing that it would refuse', async () => {
    install(before(), OFF)
    await open()
    const section = host.querySelector('.colibri') as HTMLElement
    expect(section.textContent).toBe('The HunterAI server that answers is from before it knew colibri. Start it again '
      + '(press Ctrl+C where it runs, then scripts/start.sh), reload this page, and the address of your colibri server is set here.')
    expect(section.querySelector('input')).toBeNull()
    expect(section.querySelector('button')).toBeNull()
    expect(look).not.toHaveBeenCalled()
  })
})

describe('the model list of a chat', () => {
  const CHAT: Chat = { id: 'c1', target_id: null, title: 't', mode: 'pentesting', provider: 'abliteration', model: 'big', options: {},
    active_leaf_id: null, created_at: '', updated_at: '', workdir: null, permissions: {} }

  async function picker(providers: ProviderInfo[]): Promise<HTMLSelectElement> {
    useStore.setState({ chat: CHAT, providers, live: null })
    await act(async () => { root.render(createElement(ModelPicker)) })
    return host.querySelector('select')!
  }
  const group = (select: HTMLSelectElement, name: string) => [...select.querySelectorAll('optgroup')].find((g) => g.label.startsWith(name))!

  it('offers the model colibri serves', async () => {
    const select = await picker([{ name: 'colibri', configured: true, models: [MODEL] }])
    expect(group(select, 'colibri').label).toBe('colibri')
    expect([...group(select, 'colibri').querySelectorAll('option')].map((o) => [o.textContent, o.disabled])).toEqual([['qwen3.6-colibri', false]])
  })

  it('says why colibri offers none, where the owner looks for it', async () => {
    const select = await picker([{ name: 'colibri', configured: true, models: [], note: `nothing answers at ${AT}` }])
    expect([...group(select, 'colibri').querySelectorAll('option')].map((o) => [o.textContent, o.disabled])).toEqual([[`nothing answers at ${AT}`, true]])
  })

  it('says where to set colibri up', async () => {
    const select = await picker([{ name: 'colibri', configured: false, models: [] }])
    expect(group(select, 'colibri').label).toBe('colibri — set its address in Settings')
    expect(group(select, 'colibri').querySelector('option')?.textContent).toBe('not configured')
  })
})
