// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { cloudLine } from '../bench'
import { ModelPicker } from '../components/ModelPicker'
import { SettingsDialog } from '../components/SettingsDialog'
import { useStore } from '../store'
import { llamaCppHint, machineLine, providerGroup } from '../tree'
import type { Chat, ColibriServer, LlamaModel, LlamaServer, LocalModel, ProviderInfo, Settings, SystemStatus } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const AT = 'http://127.0.0.1:9931'
const NONE = { said: '', router: false, models: [], tools: null, overhead: 9000, start_with: 20480 }
const OFF: LlamaServer = { ...NONE, url: '', state: 'off' }
const QWEN: LlamaModel = { id: 'qwen3.5-9b', context: 32768, images: true, loaded: true, failed: false, enough: null }
const SERVES: LlamaServer = { ...NONE, url: AT, state: 'ok', models: [QWEN], tools: true }
const DOWN: LlamaServer = { ...NONE, url: AT, state: 'down', said: `nothing answers at ${AT}` }
const A: LlamaModel = { id: 'story-a', context: 4096, images: false, loaded: true, failed: false, enough: null }
const B: LlamaModel = { id: 'story-b', context: 4096, images: false, loaded: false, failed: false, enough: null }
const ROUTER: LlamaServer = { ...NONE, url: AT, state: 'ok', router: true, models: [A, B] }
const MODEL = { id: 'qwen3.5-9b', label: 'qwen3.5-9b', supports_images: true, options: ['temperature', 'top_p', 'max_tokens', 'thinking'], context_window: 32768, efforts: [] }
const SMALL = 'Too small here: with the rules and tools, less than 8,192 tokens are left for the chat'
const NO_TOOLS = "The chat template of this model has no tool calls, so a chat with it cannot use HunterAI's tools: start llama-server "
  + 'with a model whose template has them, or give it one (--chat-template-file).'

describe('what Settings says under the address of llama-server', () => {
  it('says nothing while no address is set or the server was not asked', () => {
    expect(llamaCppHint(null, false)).toEqual({ lines: [], warn: null })
    expect(llamaCppHint(OFF, false)).toEqual({ lines: [], warn: null })
  })

  it('names the model it serves, its context and what it reads, and what the rules and tools take of it', () => {
    expect(llamaCppHint(SERVES, false)).toEqual({
      lines: ['It serves qwen3.5-9b: a context of 32,768 tokens, reads pictures. Pick it in the model list of a chat.',
        "HunterAI's rules and tools take about 9,000 tokens of that context."],
      warn: null,
    })
    expect(llamaCppHint({ ...SERVES, models: [{ ...QWEN, images: false, context: null }] }, false).lines[0])
      .toBe('It serves qwen3.5-9b: a context it does not name, reads text only. Pick it in the model list of a chat.')
  })

  it('says how to start a llama-server that does not answer, with a context that leaves room for a chat here', () => {
    expect(llamaCppHint(DOWN, false)).toEqual({
      lines: [`Nothing answers at ${AT}. Start llama-server (llama-server -m your-model.gguf -c 20480), then press Check.`], warn: null })
    expect(llamaCppHint({ ...DOWN, start_with: 36864 }, false).lines[0]).toContain('llama-server -m your-model.gguf -c 36864)')
  })

  it('says without alarm that it is still loading its model', () => {
    expect(llamaCppHint({ ...NONE, url: AT, state: 'loading', said: 'it is still loading its model' }, false)).toEqual({
      lines: [`llama-server at ${AT} is still loading its model. Press Check in a moment.`], warn: null })
  })

  it('says which key is wanted, by whether one is set', () => {
    const wants: LlamaServer = { ...NONE, url: AT, state: 'key', said: 'it asks for a key' }
    expect(llamaCppHint(wants, false)).toEqual({ lines: [], warn: `llama-server at ${AT} asks for a key: enter the one it was started with (--api-key) below.` })
    expect(llamaCppHint(wants, true).warn).toBe(`llama-server at ${AT} did not accept the key that is set: enter the one it was started with (--api-key) below.`)
  })

  it('passes on what else stands in the way, in the words of the server', () => {
    expect(llamaCppHint({ ...NONE, url: AT, state: 'odd', said: `what answers at ${AT} is not llama-server (it lists no model)` }, false))
      .toEqual({ lines: [], warn: `llama.cpp: what answers at ${AT} is not llama-server (it lists no model).` })
    expect(llamaCppHint({ ...NONE, url: AT, state: 'nomodel', said: 'it is still downloading big' }, false).warn).toBe('llama.cpp: it is still downloading big.')
  })

  it('warns when the chat template has no tool calls, and only when the server says so', () => {
    expect(llamaCppHint({ ...SERVES, tools: false }, false).warn).toBe(NO_TOOLS)
    expect(llamaCppHint({ ...SERVES, tools: null }, false).warn).toBeNull()
    expect(llamaCppHint({ ...SERVES, tools: false }, false).lines).toHaveLength(2)      // what it serves is said all the same
  })

  it('warns when its context leaves no room for a chat, with what to start it with', () => {
    expect(llamaCppHint({ ...SERVES, models: [{ ...QWEN, context: 16384, enough: 20480 }] }, false).warn).toBe(
      `${SMALL}. Start llama-server with a larger context: -c 20480 (a larger context needs more memory).`)
  })

  it('says both when the template has no tools and the context is too small', () => {
    expect(llamaCppHint({ ...SERVES, tools: false, models: [{ ...QWEN, context: 16384, enough: 20480 }] }, false).warn).toBe(
      `${NO_TOOLS} ${SMALL}. Start llama-server with a larger context: -c 20480 (a larger context needs more memory).`)
  })

  it('names the models of a router, each with its context and whether it is loaded', () => {
    expect(llamaCppHint(ROUTER, false)).toEqual({
      lines: ['It is a router with 2 models and loads the one a chat asks for: story-a (4,096 tokens, reads text only, loaded); '
        + 'story-b (4,096 tokens, reads text only, not loaded). Pick one in the model list of a chat.',
        "HunterAI's rules and tools take about 9,000 tokens of a model's context."],
      warn: null,
    })
    const eyes = { ...A, id: 'eyes', images: true, context: null }
    expect(llamaCppHint({ ...ROUTER, models: [eyes] }, false).lines[0])
      .toBe('It is a router with 1 model and loads the one a chat asks for: eyes (context not named, reads pictures, loaded). Pick one in the model list of a chat.')
  })

  it('names six models of a router and counts the rest', () => {
    const many = Array.from({ length: 8 }, (_, i) => ({ ...B, id: `m${i + 1}` }))
    const line = llamaCppHint({ ...ROUTER, models: many }, false).lines[0]
    expect(line).toContain('It is a router with 8 models')
    expect(line).toContain('m6 (4,096 tokens, reads text only, not loaded); and 2 more. Pick one')
    expect(line).not.toContain('m7')
    expect(llamaCppHint({ ...ROUTER, models: many.slice(0, 6) }, false).lines[0]).toContain('m6 (4,096 tokens, reads text only, not loaded). Pick one')
  })

  it('names the models of a router whose context is too small, and the one that did not load', () => {
    const small = { ...A, enough: 20480 }
    expect(llamaCppHint({ ...ROUTER, models: [small, { ...B, enough: 20480 }, { ...B, id: 'wide', context: 32768 }] }, false).warn).toBe(
      `${SMALL} with story-a, story-b. Start llama-server with a larger context: -c 20480 (a larger context needs more memory).`)
    const broke = { ...B, id: 'broke', failed: true }
    const got = llamaCppHint({ ...ROUTER, models: [A, broke] }, false)
    expect(got.lines[0]).toContain('broke (4,096 tokens, reads text only, did not load)')
    expect(got.warn).toBe('broke did not load: llama-server says why in the terminal it runs in.')
    expect(llamaCppHint({ ...ROUTER, models: [small, broke] }, false).warn).toBe(
      `${SMALL} with story-a. Start llama-server with a larger context: -c 20480 (a larger context needs more memory). `
      + 'broke did not load: llama-server says why in the terminal it runs in.')
  })
})

describe('llamacpp in the list of models', () => {
  const entry = (more: Partial<ProviderInfo>): ProviderInfo => ({ name: 'llamacpp', configured: true, models: [], ...more })

  it('says where to set it up while it has no address', () => {
    expect(providerGroup(entry({ configured: false }))).toEqual({ label: 'llamacpp — set its address in Settings', empty: 'not configured' })
  })

  it('says why it offers no model', () => {
    expect(providerGroup(entry({ note: `nothing answers at ${AT}` }))).toEqual({ label: 'llamacpp', empty: `nothing answers at ${AT}` })
  })

  it('has nothing to say when it serves one', () => {
    expect(providerGroup(entry({ models: [MODEL] }))).toEqual({ label: 'llamacpp', empty: null })
  })

  it('leaves colibri and the providers with a key as they were', () => {
    expect(providerGroup({ name: 'colibri', configured: false, models: [] }).label).toBe('colibri — set its address in Settings')
    expect(providerGroup({ name: 'openai', configured: false, models: [] }).label).toBe('openai — add key in settings')
  })
})

describe('the line at the bottom of the sidebar', () => {
  const QUIET: SystemStatus = { cpu_temp: 55, cpu_limit: 90, local_models: [], restart_needed: false, stopped: [], page: null }
  const OLLAMA = { name: 'qwen3:8b', vram_mb: 5120, size_mb: 5120, processor: '100% GPU', context: 16384 }
  const llamacpp = [{ model: 'qwen3.5-9b', context: 32768 }]

  it('names the model a llama-server holds, of which Ollama knows nothing', () => {
    expect(machineLine({ ...QUIET, llamacpp }).text).toBe('CPU 55°C · qwen3.5-9b loaded (llama.cpp)')
    expect(machineLine({ ...QUIET, local_models: null, llamacpp }).text).toBe('CPU 55°C · Ollama is not running · qwen3.5-9b loaded (llama.cpp)')
    expect(machineLine({ ...QUIET, llamacpp }).about).toBe('qwen3.5-9b: held by llama-server, with a context of 32,768 tokens.')
    expect(machineLine({ ...QUIET, llamacpp: [{ model: 'm', context: null }] }).about).toBe('m: held by llama-server.')
    expect(machineLine({ ...QUIET, llamacpp }).level).toBe('')
  })

  it('names every model a router has loaded', () => {
    const two = [...llamacpp, { model: 'story-a', context: 4096 }]
    expect(machineLine({ ...QUIET, llamacpp: two }).text).toBe('CPU 55°C · qwen3.5-9b (llama.cpp), story-a (llama.cpp) loaded')
    expect(machineLine({ ...QUIET, llamacpp: two }).about).toBe('qwen3.5-9b: held by llama-server, with a context of 32,768 tokens. '
      + 'story-a: held by llama-server, with a context of 4,096 tokens.')
  })

  it('says no local model is loaded for a router that has loaded none, and without a llama-server', () => {
    expect(machineLine({ ...QUIET, llamacpp: [] }).text).toBe('CPU 55°C · no local model loaded')
    expect(machineLine({ ...QUIET, llamacpp: null }).text).toBe('CPU 55°C · no local model loaded')
    expect(machineLine(QUIET).text).toBe('CPU 55°C · no local model loaded')                       // a server from before it asked llama-server
    expect(machineLine({ ...QUIET, llamacpp: [] }).about).toBe('')
  })

  it('stands beside the model colibri holds', () => {
    expect(machineLine({ ...QUIET, colibri: { model: 'big', context: 65536 }, llamacpp }).text).toBe('CPU 55°C · big (colibri), qwen3.5-9b (llama.cpp) loaded')
  })

  it('says that Ollama and llama-server share the memory when each holds a model, and how to have one at a time', () => {
    const both = machineLine({ ...QUIET, local_models: [OLLAMA, { ...OLLAMA, name: 'bge-m3:latest' }], llamacpp })
    expect(both.text).toBe('CPU 55°C · qwen3:8b (5.0 GB, GPU), bge-m3:latest (5.0 GB, GPU), qwen3.5-9b (llama.cpp) loaded')
    expect(both.about).toBe('qwen3:8b: 5.0 GB in memory, 100% GPU as `ollama ps` says it, loaded with a window of 16,384 tokens. '
      + 'bge-m3:latest: 5.0 GB in memory, 100% GPU as `ollama ps` says it, loaded with a window of 16,384 tokens. '
      + 'qwen3.5-9b: held by llama-server, with a context of 32,768 tokens. '
      + 'Ollama and llama-server each hold a model now and share the memory of this machine: what does not fit the graphics card '
      + 'runs on the processor, which is slow. To have one at a time, unload Ollama\'s (`ollama stop qwen3:8b`, `ollama stop bge-m3:latest`) or stop llama-server.')
    expect(both.level).toBe('')
  })

  it('does not say that of one program alone', () => {
    expect(machineLine({ ...QUIET, local_models: [OLLAMA] }).about).not.toContain('share the memory')
    expect(machineLine({ ...QUIET, llamacpp }).about).not.toContain('share the memory')
    expect(machineLine({ ...QUIET, local_models: [OLLAMA], llamacpp: [] }).about).not.toContain('share the memory')
    expect(machineLine({ ...QUIET, local_models: [OLLAMA], colibri: { model: 'big', context: null } }).about).not.toContain('share the memory')
  })
})

describe('the line of a bench about where the prompts go', () => {
  it('counts a llama-server as this machine', () => {
    expect(cloudLine([{ provider: 'llamacpp', model: 'qwen3.5-9b' }, { provider: 'ollama', model: 'qwen3.5:9b' }])).toBeNull()
    expect(cloudLine([{ provider: 'llamacpp', model: 'qwen3.5-9b' }, { provider: 'openai', model: 'm' }])).toBe('The prompts of this set are sent to openai.')
  })
})

const SETTINGS: Settings = {
  default_provider: 'abliteration', default_model: 'big', fallback_model: 'qwen3:8b', local_window: 16384, local_window_max: 131072,
  keys: { ABLIT_API_KEY: true }, permissions: {}, max_turns: 60, swarm_width: 3, swarm_width_max: 6,
  machine: { cores: 8, memory_gb: 32 }, presets: {}, decision_provider: null, decision_model: null, decision_gate: false,
  decision_engine: true, kb_redact: true, kb_skip_generated: true, vision_model: null, browser_via_caido: false, answer_check: false,
  mcp_on_demand: true, notify_findings: false, notify_min_severity: 'high', notify_webhook_set: false, caido_available: false,
  upstream_proxy: '', upstream_proxy_password: false, upstream_proxy_intercepts: false, upstream_proxy_kb: false,
  provider_proxy: '', provider_proxy_password: false, colibri_url: '', colibri_key_set: false, llamacpp_url: '', llamacpp_key_set: false,
  interface_font: '', subagent_providers: ['openai'],
}
const LOCAL: LocalModel = {
  answers: true, model: 'qwen3:8b', installed: true, window: 16384, limit: 40960, bytes_per_token: 147456,
  overhead: { tokens: 6752, tools: 36, on_demand: 0 }, enough: 16384, loaded: null,
}
const COLIBRI_OFF: ColibriServer = { url: '', state: 'off', said: '', model: null, context: null, images: false, overhead: null, enough: null }

let root: Root
let host: HTMLElement
let put: ReturnType<typeof vi.spyOn>
let look: ReturnType<typeof vi.spyOn>
let colibri: ReturnType<typeof vi.spyOn>
let listed: ReturnType<typeof vi.spyOn>

function install(settings: Settings, server: LlamaServer): void {
  useStore.setState({ settings, providers: [], error: null })
  vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
  look = vi.spyOn(api, 'systemLlamaCpp').mockResolvedValue(server)
}

beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  vi.spyOn(api, 'listTools').mockResolvedValue([])
  vi.spyOn(api, 'mcpStatus').mockResolvedValue({ configured: [], servers: [], error: null })
  vi.spyOn(api, 'systemLocal').mockResolvedValue(LOCAL)
  colibri = vi.spyOn(api, 'systemColibri').mockResolvedValue(COLIBRI_OFF)
  listed = vi.spyOn(api, 'listProviders').mockResolvedValue([{ name: 'llamacpp', configured: true, models: [MODEL] }])
  put = vi.spyOn(api, 'putSettings').mockImplementation(async (patch) => ({ ...SETTINGS, ...patch, llamacpp_url: AT }) as Settings)
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
const address = () => host.querySelector<HTMLInputElement>('input[aria-label="llama.cpp address"]')!
const section = () => address().closest('.llamacpp') as HTMLElement
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

describe('asking the server what answers at the address', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('asks the endpoint for llama-server, and sends nothing', async () => {
    look.mockRestore()                                    // the call itself, not a stand-in
    const fetched = vi.fn(async () => new Response(JSON.stringify(SERVES), { status: 200, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetched)
    expect(await api.systemLlamaCpp()).toEqual(SERVES)
    expect(fetched).toHaveBeenCalledTimes(1)
    const [path, how] = fetched.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe('/api/system/llamacpp')
    expect(how.method).toBe('GET')
    expect(how.body).toBeUndefined()
  })
})

describe('llama.cpp in Settings', () => {
  it('is not used and not asked until the owner gives its address', async () => {
    await open()
    expect(address().value).toBe('')
    expect(address().placeholder).toBe(AT)
    expect(section().textContent).toContain('Address of your llama-server · none')
    expect(look).not.toHaveBeenCalled()
    expect(button('Clear').disabled).toBe(true)
    expect(button('Check').disabled).toBe(true)
  })

  it('stands under its own heading, after colibri', async () => {
    await open()
    const headings = [...host.querySelectorAll('h3.section')].map((h) => h.textContent)
    expect(headings.indexOf('llama.cpp')).toBe(headings.indexOf('colibri') + 1)
    expect(section().previousElementSibling?.textContent).toBe('llama.cpp')
  })

  it('saves an address, shows it as it is reached, and says what answers there', async () => {
    look.mockResolvedValue(SERVES)
    await open()
    await type(address(), ' 127.0.0.1:9931/v1 ')
    await press(button('Save'))
    expect(put).toHaveBeenCalledWith({ llamacpp_url: ' 127.0.0.1:9931/v1 ' })
    expect(address().value).toBe(AT)
    expect(section().textContent).toContain('Address of your llama-server · in use')
    expect(section().textContent).toContain('It serves qwen3.5-9b: a context of 32,768 tokens, reads pictures.')
    expect(section().querySelector('[role="alert"]')).toBeNull()
    expect(listed).toHaveBeenCalled()                     // the model list of the chats has it at once
    expect(useStore.getState().providers[0].models[0].id).toBe('qwen3.5-9b')
    expect(useStore.getState().settings?.llamacpp_url).toBe(AT)
  })

  it('says why the server refused an address, and keeps what was typed', async () => {
    put.mockRejectedValue(new Error('llama.cpp address: it has to begin with http:// or https://'))
    await open()
    await type(address(), 'ftp://127.0.0.1:9931')
    await press(button('Save'))
    expect(section().querySelector('[role="alert"]')?.textContent).toBe('llama.cpp address: it has to begin with http:// or https://')
    expect(address().value).toBe('ftp://127.0.0.1:9931')
    expect(look).not.toHaveBeenCalled()
  })

  it('asks the server when Settings opens with an address, says how to start it, and asks again on Check', async () => {
    install({ ...SETTINGS, llamacpp_url: AT }, DOWN)
    await open()
    expect(look).toHaveBeenCalledTimes(1)
    expect(section().textContent).toContain(`Nothing answers at ${AT}. Start llama-server (llama-server -m your-model.gguf -c 20480), then press Check.`)
    expect(section().querySelector('[role="alert"]')).toBeNull()
    look.mockResolvedValue(SERVES)                        // the owner started it
    listed.mockClear()
    await press(button('Check'))
    expect(look).toHaveBeenCalledTimes(2)
    expect(section().textContent).toContain('It serves qwen3.5-9b')
    expect(section().textContent).not.toContain('Nothing answers')
    expect(listed).toHaveBeenCalledTimes(1)
  })

  it('stops saying what answers at the saved address while another one is being typed', async () => {
    install({ ...SETTINGS, llamacpp_url: AT }, { ...SERVES, models: [{ ...QWEN, context: 16384, enough: 20480 }] })
    await open()
    expect(section().textContent).toContain('It serves qwen3.5-9b')
    expect(section().querySelector('[role="alert"]')?.textContent).toContain('Start llama-server with a larger context: -c 20480')
    expect(button('Check').disabled).toBe(false)
    await type(address(), 'http://127.0.0.1:8080')
    expect(section().textContent).not.toContain('It serves qwen3.5-9b')
    expect(section().querySelector('[role="alert"]')).toBeNull()
    expect(button('Check').disabled).toBe(true)           // Check asks the saved address, and that is not the one in the field
    expect(button('Save').disabled).toBe(false)
  })

  it('clears the address when the field is emptied', async () => {
    install({ ...SETTINGS, llamacpp_url: AT }, SERVES)
    put.mockImplementation(async (patch: object) => ({ ...SETTINGS, ...patch, llamacpp_url: '' }) as Settings)
    await open()
    listed.mockClear()
    await type(address(), '')
    await press(button('Clear'))
    expect(put).toHaveBeenCalledWith({ llamacpp_url: '' })
    expect(address().value).toBe('')
    expect(listed).toHaveBeenCalledTimes(1)               // the model list of the chats no longer offers what it served
    expect(section().textContent).not.toContain('It serves qwen3.5-9b')
    expect(section().textContent).toContain('Address of your llama-server · none')
  })

  it('takes the key llama-server was started with, beside the address and not among the cloud keys', async () => {
    const key = vi.spyOn(api, 'putKey').mockResolvedValue(undefined)
    install({ ...SETTINGS, llamacpp_url: AT }, { ...NONE, url: AT, state: 'key', said: 'it asks for a key' })
    await open()
    expect(section().querySelector('[role="alert"]')?.textContent).toBe(`llama-server at ${AT} asks for a key: enter the one it was started with (--api-key) below.`)
    const field = section().querySelector<HTMLInputElement>('input[type="password"]')!
    expect(keyRow().textContent).toContain('llama.cpp key, only when llama-server was started with --api-key LLAMACPP_API_KEY · not set')
    vi.spyOn(api, 'getSettings').mockResolvedValue({ ...SETTINGS, llamacpp_url: AT, llamacpp_key_set: true })
    look.mockResolvedValue(SERVES)
    await type(field, 'secret-key-1')
    await press([...section().querySelectorAll('button')].filter((b) => b.textContent === 'Save').pop()!)
    expect(key).toHaveBeenCalledWith('LLAMACPP_API_KEY', 'secret-key-1')
    expect(section().textContent).toContain('It serves qwen3.5-9b')
    expect(section().querySelector('[role="alert"]')).toBeNull()
    expect(field.value).toBe('')                          // a key is never left on the page
    expect(keyRow().textContent).toContain('LLAMACPP_API_KEY · set')
    expect([...host.querySelectorAll('input[type="password"]')].filter((i) => !i.closest('.llamacpp') && i.closest('.keyrow')?.textContent?.includes('LLAMACPP'))).toEqual([])
  })

  it('says that the key which is set was not accepted, and not that one is wanted', async () => {
    install({ ...SETTINGS, llamacpp_url: AT, llamacpp_key_set: true }, { ...NONE, url: AT, state: 'key', said: 'it did not accept the key that is set under Settings' })
    await open()
    expect(section().querySelector('[role="alert"]')?.textContent).toBe(`llama-server at ${AT} did not accept the key that is set: enter the one it was started with (--api-key) below.`)
    expect(keyRow().textContent).toContain('· set')
  })

  it('still shows the field when the server cannot be asked what answers there', async () => {
    install({ ...SETTINGS, llamacpp_url: AT }, SERVES)
    look.mockRejectedValue(new Error('404: Not Found'))   // a server from before it could say
    await open()
    expect(address().value).toBe(AT)
    expect(section().querySelector('[role="alert"]')).toBeNull()
  })

  it('asks each of the two servers only when it has an address of its own', async () => {
    install({ ...SETTINGS, llamacpp_url: AT }, SERVES)
    await open()
    expect(look).toHaveBeenCalledTimes(1)
    expect(colibri).not.toHaveBeenCalled()
    expect(host.querySelector<HTMLInputElement>('input[aria-label="colibri address"]')!.value).toBe('')
    expect(host.querySelector('.colibri')!.textContent).not.toContain('qwen3.5-9b')
  })

  it('says what llama-server is, how it is started and where its address goes', async () => {
    await open()
    const about = section().lastElementChild!.textContent!
    expect(about).toContain('llama-server is the server of llama.cpp (github.com/ggml-org/llama.cpp)')
    expect(about).toContain('port 9931')
    expect(about).toContain('--models-dir')
    expect(about).toContain('Empty: llama.cpp is not used. Written to .env as HUNTERAI_LLAMACPP_URL.')
  })

  it('is named where Settings speaks of the local models and of what is never sent through a proxy', async () => {
    await open()
    expect(host.textContent).toContain('A chat on a local model (Ollama, colibri, llama.cpp) has no subagents by itself')
    expect(host.textContent).toContain('Ollama on this machine, your colibri server and your llama-server are never sent through it.')
  })
})

describe('Settings on a server from before it knew llama.cpp', () => {
  /** What such a server sends: everything but the two fields this page added. The owner updated, the page is the
   * new one, and the server that answers was not started again yet. */
  const before = (): Settings => {
    const { llamacpp_url: _url, llamacpp_key_set: _key, ...older } = SETTINGS
    void _url; void _key
    return older as Settings
  }

  it('says under llama.cpp that the server has to be started again, and offers nothing that it would refuse', async () => {
    install(before(), OFF)
    await open()
    const part = host.querySelector('.llamacpp') as HTMLElement
    expect(part.textContent).toBe('The HunterAI server that answers is from before it knew llama.cpp. Start it again '
      + '(press Ctrl+C where it runs, then scripts/start.sh), reload this page, and the address of your llama-server is set here.')
    expect(part.querySelector('input')).toBeNull()
    expect(part.querySelector('button')).toBeNull()
    expect(look).not.toHaveBeenCalled()
    expect(host.querySelector('input[aria-label="colibri address"]')).not.toBeNull()       // colibri, which that server knows, is as it was
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
  const group = (select: HTMLSelectElement) => [...select.querySelectorAll('optgroup')].find((g) => g.label.startsWith('llamacpp'))!

  it('offers every model a llama-server lists', async () => {
    const select = await picker([{ name: 'llamacpp', configured: true, models: [MODEL, { ...MODEL, id: 'story-a', label: 'story-a' }] }])
    expect(group(select).label).toBe('llamacpp')
    expect([...group(select).querySelectorAll('option')].map((o) => [o.textContent, o.disabled])).toEqual([['qwen3.5-9b', false], ['story-a', false]])
  })

  it('says where to set llama.cpp up', async () => {
    const select = await picker([{ name: 'llamacpp', configured: false, models: [] }])
    expect(group(select).label).toBe('llamacpp — set its address in Settings')
    expect(group(select).querySelector('option')?.textContent).toBe('not configured')
  })
})
