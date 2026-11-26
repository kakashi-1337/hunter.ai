// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { SettingsDialog } from '../components/SettingsDialog'
import { useStore } from '../store'
import { llamaCppHint, llamaFindView, llamaModelNote } from '../tree'
import type { ColibriServer, LlamaFind, LlamaFindModel, LlamaServer, LocalModel, Settings } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const AT = 'http://127.0.0.1:9931'
const LIB = '/usr/local/lib/ollama'
const START = `LD_LIBRARY_PATH=${LIB}:${LIB}/cuda_v13 GGML_BACKEND_PATH=${LIB}/cuda_v13/libggml-cuda.so ${LIB}/llama-server`
const QWEN: LlamaFindModel = { name: 'huihui-qwen3.5-9b:latest', alias: 'huihui-qwen3.5-9b', images: true, why_not: '', note: '',
  command: `${START} -m /home/dave/.ollama/models/blobs/sha256-aa --mmproj /home/dave/.ollama/models/blobs/sha256-bb -c 32768 --alias huihui-qwen3.5-9b --port 9931` }
const TINY: LlamaFindModel = { name: 'qwen3:8b', alias: 'qwen3-8b', images: false, why_not: '',
  note: 'Ollama keeps this model in the form of its own engine, not in the one of llama.cpp, so llama-server may not load its file; it says why in its terminal.',
  command: `${START} -m /home/dave/.ollama/models/blobs/sha256-cc -c 32768 --alias qwen3-8b --port 9931 --temp 0.6` }
const CLOUD: LlamaFindModel = { name: 'big:cloud', alias: 'big-cloud', images: false, command: null, note: '',
  why_not: 'Ollama names no file on this machine for it (FROM big:cloud)' }
const CARD = { name: 'CUDA0', card: 'NVIDIA GeForce RTX 5080 Laptop GPU', total_mb: 15839, free_mb: 6015 }
const FIND: LlamaFind = { address: AT, program: `${LIB}/llama-server`, from_ollama: true, devices: [CARD], answers: null, context: 32768,
  ollama: true, held: [], plain: `${START} -m your-model.gguf -c 32768 --port 9931`, models: [QWEN, TINY] }
const NONE = { said: '', router: false, models: [], tools: null, overhead: 20537, start_with: 32768 }
const DOWN: LlamaServer = { ...NONE, url: AT, state: 'down', said: `nothing answers at ${AT}` }
const NO_MODEL: LlamaServer = { ...NONE, url: AT, state: 'nomodel', said: 'it lists no model' }
const ODD: LlamaServer = { ...NONE, url: AT, state: 'odd', said: `what answers at ${AT} is not llama-server (it answers HTTP 404)` }
const SERVES: LlamaServer = { ...NONE, url: AT, state: 'ok', tools: true,
  models: [{ id: 'huihui-qwen3.5-9b', context: 32768, images: true, loaded: true, failed: false, enough: null }] }

const FOUND = `llama-server is at ${LIB}/llama-server, the one Ollama brings. It sees CUDA0: NVIDIA GeForce RTX 5080 Laptop GPU, 6,015 of 15,839 MiB free.`
const WITH_OLLAMAS = "Start it with a model Ollama has: llama-server reads Ollama's own file, nothing is copied."
const RUN_IT = 'Run the command in a terminal and leave it open: llama-server holds the model until you stop it there (Ctrl+C).'
const NO_CARD = 'This llama-server sees no graphics card: its build has no part for one, or the driver is missing. A model started with it '
  + 'runs on the processor, much slower than on a card. The README says where a build for your card comes from ("llama-server on your graphics card").'
const TAKEN = `A llama-server already runs at ${AT} without a model to chat with, so the port is taken: stop it first (Ctrl+C in the terminal it runs in).`

describe('what Settings shows to start a llama-server on this machine', () => {
  it('names the program and its card, offers the models of Ollama, and says what to do with the command', () => {
    expect(llamaFindView(FIND, null, '')).toEqual({
      lines: [FOUND, WITH_OLLAMAS], warn: null, use: AT, models: [QWEN, TINY], plain: FIND.plain,
      after: [RUN_IT, `When it says it is listening, press "Use ${AT}".`],
    })
  })

  it('shows nothing before the machine was looked at, and for an address on another machine', () => {
    expect(llamaFindView(null, null, '')).toBeNull()
    expect(llamaFindView({ ...FIND, address: null }, DOWN, 'http://192.168.1.20:9931')).toBeNull()
  })

  it('shows nothing for a saved address that was not asked yet, or at which a llama-server serves, loads or wants its key', () => {
    expect(llamaFindView(FIND, null, AT)).toBeNull()
    expect(llamaFindView(FIND, SERVES, AT)).toBeNull()
    expect(llamaFindView(FIND, { ...NONE, url: AT, state: 'loading' }, AT)).toBeNull()
    expect(llamaFindView(FIND, { ...NONE, url: AT, state: 'key' }, AT)).toBeNull()
  })

  it('tells to press Check, and offers no button, where the address is saved and nothing answers at it', () => {
    const view = llamaFindView(FIND, DOWN, AT)!
    expect(view.use).toBeNull()
    expect(view.after).toEqual([RUN_IT, 'When it says it is listening, press Check.'])
    expect(view.lines).toEqual([FOUND, WITH_OLLAMAS])
    expect(view.warn).toBeNull()
  })

  it('offers the address of a llama-server that already answers there, and nothing else', () => {
    const serves = llamaFindView({ ...FIND, answers: { state: 'ok', serves: ['qwythos-9b', 'tiny'], said: '' } }, null, '')
    expect(serves).toEqual({ lines: [`A llama-server answers at ${AT} and serves qwythos-9b, tiny.`], warn: null, use: AT, models: [], plain: null, after: [] })
    const loading = llamaFindView({ ...FIND, answers: { state: 'loading', serves: [], said: '' } }, null, '')!
    expect(loading.lines).toEqual([`A llama-server answers at ${AT} and is still loading its model.`])
    expect(loading.use).toBe(AT)
    const key = llamaFindView({ ...FIND, answers: { state: 'key', serves: [], said: '' } }, null, '')!
    expect(key.lines).toEqual([`A llama-server answers at ${AT} and asks for a key: save the address, then enter the key below.`])
    expect(key.use).toBe(AT)
    expect(key.models).toEqual([])
  })

  it('says to stop a llama-server that runs there without a model, by what the search or the saved address found', () => {
    const searched = llamaFindView({ ...FIND, answers: { state: 'nomodel', serves: [], said: 'it lists no model' } }, null, '')!
    expect(searched.warn).toBe(TAKEN)
    expect(searched.models).toEqual([QWEN, TINY])
    expect(searched.use).toBe(AT)
    expect(llamaFindView(FIND, NO_MODEL, AT)!.warn).toBe(TAKEN)
  })

  it('says that the port is taken where something else answers there', () => {
    const other = `Something that is not llama-server answers at ${AT}, so that port is taken: change --port in the command, and save the address with the port you chose.`
    expect(llamaFindView({ ...FIND, answers: { state: 'odd', serves: [], said: 'x' } }, null, '')!.warn).toBe(other)
    expect(llamaFindView(FIND, ODD, AT)!.warn).toBe(other)
  })

  it('warns of a llama-server that sees no graphics card, and says nothing of cards where it does not tell', () => {
    const blind = llamaFindView({ ...FIND, program: '/home/dave/.unsloth/llama.cpp/llama-server', from_ollama: false, devices: [] }, null, '')!
    expect(blind.lines[0]).toBe('llama-server is at /home/dave/.unsloth/llama.cpp/llama-server.')
    expect(blind.warn).toBe(NO_CARD)
    const silent = llamaFindView({ ...FIND, devices: null }, null, '')!
    expect(silent.lines[0]).toBe(`llama-server is at ${LIB}/llama-server, the one Ollama brings.`)
    expect(silent.warn).toBeNull()
    expect(llamaFindView({ ...FIND, devices: [], answers: { state: 'nomodel', serves: [], said: '' } }, null, '')!.warn).toBe(`${TAKEN} ${NO_CARD}`)
  })

  it('names every card a llama-server sees', () => {
    const two = llamaFindView({ ...FIND, devices: [CARD, { name: 'Vulkan1', card: 'Intel(R) Graphics (ARL)', total_mb: 15885, free_mb: 15885 }] }, null, '')!
    expect(two.lines[0]).toBe(`llama-server is at ${LIB}/llama-server, the one Ollama brings. It sees CUDA0: NVIDIA GeForce RTX 5080 Laptop GPU, `
      + '6,015 of 15,839 MiB free; Vulkan1: Intel(R) Graphics (ARL), 15,885 of 15,885 MiB free.')
  })

  it('says to free the card of what Ollama holds before the command is run', () => {
    const view = llamaFindView({ ...FIND, held: ['huihui-qwen3.5-9b:latest', 'qwen3:8b'] }, null, '')!
    expect(view.after).toEqual([
      'Ollama holds huihui-qwen3.5-9b:latest, qwen3:8b in memory now, and both would share the card. Free it first: ollama stop huihui-qwen3.5-9b:latest && ollama stop qwen3:8b',
      RUN_IT, `When it says it is listening, press "Use ${AT}".`])
  })

  it('leaves the command for a file of the owner where Ollama does not answer or has no chat model', () => {
    const silent = llamaFindView({ ...FIND, ollama: false, models: [] }, null, '')!
    expect(silent.lines).toEqual([FOUND, 'Ollama does not answer, so the models it has cannot be offered here.'])
    expect(silent.plain).toBe(FIND.plain)
    expect(silent.models).toEqual([])
    expect(llamaFindView({ ...FIND, models: [] }, null, '')!.lines).toEqual([FOUND, 'Ollama has no chat model to offer.'])
  })

  it('says where llama-server comes from when none was found, and offers neither a command nor a button', () => {
    const none = llamaFindView({ ...FIND, program: null, from_ollama: false, devices: null, plain: null, models: [], ollama: false }, null, '')!
    expect(none).toEqual({
      lines: ["No llama-server was found on this machine: none in the llama.cpp folder of HunterAI's data folder, none on the PATH, none where "
        + 'Unsloth Studio keeps its own, and none beside Ollama. It comes with llama.cpp (github.com/ggml-org/llama.cpp) and with a current '
        + 'Ollama; the README says more ("llama-server on your graphics card"). HunterAI does not install it.'],
      warn: null, use: null, models: [], plain: null, after: [],
    })
    expect(llamaFindView({ ...FIND, program: null, plain: null, models: [], answers: { state: 'nomodel', serves: [], said: '' } }, null, '')!.warn).toBe(TAKEN)
    // No card is said of a program only: an answer that names no program and an empty list of cards warns of nothing.
    expect(llamaFindView({ ...FIND, program: null, devices: [], plain: null, models: [] }, null, '')!.warn).toBeNull()
  })
})

describe('what stands under the command of the model that is picked', () => {
  it('says how the model is named in the list and what it reads', () => {
    expect(llamaModelNote(QWEN)).toEqual({ lines: ['In the model list of a chat it will be huihui-qwen3.5-9b, under llamacpp; it reads pictures.'], warn: null })
    expect(llamaModelNote(TINY)).toEqual({ lines: ['In the model list of a chat it will be qwen3-8b, under llamacpp; it reads text only.', TINY.note], warn: null })
  })

  it('says why a model cannot be started this way', () => {
    expect(llamaModelNote(CLOUD)).toEqual({ lines: [], warn: 'big:cloud cannot be started this way: Ollama names no file on this machine for it (FROM big:cloud).' })
  })
})

describe('the line for a llama-server that does not answer', () => {
  it('points at the command below where one is shown, and gives the plain one where none is', () => {
    expect(llamaCppHint(DOWN, false, true)).toEqual({ lines: [`Nothing answers at ${AT}. Start llama-server with the command below, then press Check.`], warn: null })
    expect(llamaCppHint(DOWN, false)).toEqual({ lines: [`Nothing answers at ${AT}. Start llama-server (llama-server -m your-model.gguf -c 32768), then press Check.`], warn: null })
    expect(llamaCppHint(DOWN, false, false).lines[0]).toContain('llama-server -m your-model.gguf')
  })
})

// ---- in the page

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
const OFF: LlamaServer = { ...NONE, url: '', state: 'off' }

describe('the llama.cpp part of Settings', () => {
  let root: Root
  let host: HTMLElement
  let put: ReturnType<typeof vi.spyOn>
  let look: ReturnType<typeof vi.spyOn>
  let find: ReturnType<typeof vi.spyOn>
  let copied: string[]

  function install(settings: Settings, server: LlamaServer, found: LlamaFind | Error = FIND): void {
    useStore.setState({ settings, providers: [], error: null })
    vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
    look = vi.spyOn(api, 'systemLlamaCpp').mockResolvedValue(server)
    find = vi.spyOn(api, 'systemLlamaCppFind')
    if (found instanceof Error) find.mockRejectedValue(found)
    else find.mockResolvedValue(found)
  }

  beforeEach(() => {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
    copied = []
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: async (text: string) => { copied.push(text) } } })
    vi.spyOn(api, 'listTools').mockResolvedValue([])
    vi.spyOn(api, 'mcpStatus').mockResolvedValue({ configured: [], servers: [], error: null })
    vi.spyOn(api, 'systemLocal').mockResolvedValue(LOCAL)
    vi.spyOn(api, 'systemColibri').mockResolvedValue(COLIBRI_OFF)
    vi.spyOn(api, 'listProviders').mockResolvedValue([])
    put = vi.spyOn(api, 'putSettings').mockImplementation(async (patch) => ({ ...SETTINGS, ...patch }) as Settings)
  })
  afterEach(() => {
    act(() => root.unmount())
    host.remove()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  async function open(): Promise<void> {
    await act(async () => { root.render(createElement(SettingsDialog, { onClose: () => {} })) })
    await act(async () => {})
  }
  const section = () => host.querySelector<HTMLInputElement>('input[aria-label="llama.cpp address"]')!.closest('.llamacpp') as HTMLElement
  const start = () => section().querySelector('.llamastart') as HTMLElement | null
  const button = (name: string) => [...section().querySelectorAll('button')].find((b) => b.textContent === name)
  const press = (el: HTMLElement) => act(async () => { el.click() })
  const picker = () => start()!.querySelector<HTMLSelectElement>('select[aria-label="Model of Ollama to start llama-server with"]')!
  const commands = () => [...start()!.querySelectorAll('pre.command')].map((c) => c.textContent)
  const hints = () => [...start()!.querySelectorAll('.hint')].map((h) => h.textContent)

  it('looks at the machine while no address is saved, and shows the program, the models and the command of the first one', async () => {
    install(SETTINGS, OFF)
    await open()
    expect(find).toHaveBeenCalledTimes(1)
    expect(look).not.toHaveBeenCalled()
    expect(hints().slice(0, 2)).toEqual([FOUND, WITH_OLLAMAS])
    expect([...picker().options].map((o) => o.textContent)).toEqual(['huihui-qwen3.5-9b:latest', 'qwen3:8b'])
    expect(picker().value).toBe('huihui-qwen3.5-9b:latest')
    expect(commands()).toEqual([QWEN.command, FIND.plain])
    expect(hints()).toContain('In the model list of a chat it will be huihui-qwen3.5-9b, under llamacpp; it reads pictures.')
    expect(hints().slice(-2)).toEqual([RUN_IT, `When it says it is listening, press "Use ${AT}".`])
    expect(start()!.querySelector('.ask-warning')).toBeNull()
  })

  it('shows the command of the model that is picked, with what there is to know about it', async () => {
    install(SETTINGS, OFF, { ...FIND, models: [QWEN, TINY, CLOUD] })
    await open()
    await act(async () => { picker().value = 'qwen3:8b'; picker().dispatchEvent(new Event('change', { bubbles: true })) })
    expect(commands()).toEqual([TINY.command, FIND.plain])
    expect(hints()).toContain(TINY.note)
    expect(hints()).toContain('In the model list of a chat it will be qwen3-8b, under llamacpp; it reads text only.')
    expect([...picker().options].map((o) => o.textContent)).toEqual(['huihui-qwen3.5-9b:latest', 'qwen3:8b', 'big:cloud (cannot be started this way)'])
    await act(async () => { picker().value = 'big:cloud'; picker().dispatchEvent(new Event('change', { bubbles: true })) })
    expect(commands()).toEqual([FIND.plain])
    expect(start()!.querySelector('.ask-warning')!.textContent).toBe('big:cloud cannot be started this way: Ollama names no file on this machine for it (FROM big:cloud).')
  })

  it('picks the first model that can be started where the first of the list cannot', async () => {
    install(SETTINGS, OFF, { ...FIND, models: [CLOUD, TINY] })
    await open()
    expect(picker().value).toBe('qwen3:8b')
    expect(commands()[0]).toBe(TINY.command)
  })

  it('shows the first model with its reason where none of them can be started', async () => {
    install(SETTINGS, OFF, { ...FIND, models: [CLOUD] })
    await open()
    expect(picker().value).toBe('big:cloud')
    expect(commands()).toEqual([FIND.plain])
    expect(start()!.querySelector('.ask-warning')!.textContent).toBe('big:cloud cannot be started this way: Ollama names no file on this machine for it (FROM big:cloud).')
  })

  it('warns of a llama-server that sees no card, and says what the second command is for', async () => {
    install(SETTINGS, OFF, { ...FIND, devices: [] })
    await open()
    expect(start()!.querySelector('.ask-warning')!.textContent).toBe(NO_CARD)
    expect(hints()).toContain('For a GGUF file of your own, with its path in place of your-model.gguf:')
  })

  it('says Copied for a moment and then offers to copy again', async () => {
    install(SETTINGS, OFF)
    await open()
    const names = () => [...start()!.querySelectorAll('button')].map((b) => b.textContent)
    vi.useFakeTimers()
    try {
      await press([...start()!.querySelectorAll('button')].filter((b) => b.textContent === 'Copy')[0])
      expect(names()).toContain('Copied')
      await act(async () => { vi.advanceTimersByTime(1500) })
      expect(names()).not.toContain('Copied')
    } finally {
      vi.useRealTimers()
    }
  })

  it('copies the command of the model, and the one for a file of the owner, each with its own button', async () => {
    install(SETTINGS, OFF)
    await open()
    const copy = [...start()!.querySelectorAll('button')].filter((b) => b.textContent === 'Copy')
    expect(copy).toHaveLength(2)
    await press(copy[0])
    expect(copied).toEqual([QWEN.command])
    expect([...start()!.querySelectorAll('button')].map((b) => b.textContent)).toContain('Copied')
    await press([...start()!.querySelectorAll('button')].filter((b) => b.textContent === 'Copy')[0])
    expect(copied).toEqual([QWEN.command, FIND.plain])
  })

  it('saves the usual address with its button, and then asks the server there', async () => {
    install(SETTINGS, OFF)
    await open()
    look.mockResolvedValue(SERVES)
    await press(button(`Use ${AT}`)!)
    await act(async () => {})
    expect(put).toHaveBeenCalledWith({ llamacpp_url: AT })
    expect(look).toHaveBeenCalledTimes(1)
    expect(start()).toBeNull()
    expect(section().textContent).toContain('It serves huihui-qwen3.5-9b: a context of 32,768 tokens, reads pictures.')
  })

  it('says why the address could not be saved', async () => {
    install(SETTINGS, OFF)
    await open()
    put.mockRejectedValue(new Error('the .env file cannot be written'))
    await press(button(`Use ${AT}`)!)
    expect(start()!.querySelector('.ask-warning')!.textContent).toBe('the .env file cannot be written')
  })

  it('holds the button while the address is being saved, frees it when that failed, and drops the reason at the next try', async () => {
    install(SETTINGS, OFF)
    await open()
    let fail!: (reason: Error) => void
    put.mockImplementationOnce(() => new Promise<Settings>((_, reject) => { fail = reject }))
    await press(button(`Use ${AT}`)!)
    expect(button('Saving…')!.disabled).toBe(true)
    await act(async () => { fail(new Error('the .env file cannot be written')) })
    expect(button(`Use ${AT}`)!.disabled).toBe(false)
    expect(start()!.querySelector('.ask-warning')!.textContent).toBe('the .env file cannot be written')
    put.mockImplementationOnce(() => new Promise<Settings>(() => {}))
    await press(button(`Use ${AT}`)!)
    expect(start()!.querySelector('.ask-warning')).toBeNull()
  })

  it.each([
    ['serves no model', NO_MODEL, TAKEN],
    ['is not a llama-server', ODD, `Something that is not llama-server answers at ${AT}, so that port is taken: change --port in the command, and save the address with the port you chose.`],
  ])('looks at the machine when what answers at the saved address %s, and says that the port is taken', async (_, server, taken) => {
    install({ ...SETTINGS, llamacpp_url: AT }, server)
    await open()
    expect(find).toHaveBeenCalledTimes(1)
    expect(start()!.querySelector('.ask-warning')!.textContent).toBe(taken)
    expect(commands()).toEqual([QWEN.command, FIND.plain])
  })

  it('looks at the machine when nothing answers at the saved address, and points the line above at the command', async () => {
    install({ ...SETTINGS, llamacpp_url: AT }, DOWN)
    await open()
    expect(look).toHaveBeenCalledTimes(1)
    expect(find).toHaveBeenCalledTimes(1)
    expect(section().textContent).toContain(`Nothing answers at ${AT}. Start llama-server with the command below, then press Check.`)
    expect(commands()).toEqual([QWEN.command, FIND.plain])
    expect(button(`Use ${AT}`)).toBeUndefined()
    expect(hints().slice(-1)).toEqual(['When it says it is listening, press Check.'])
  })

  it('looks again after a Check that still finds nothing, and puts the block away once a model is served', async () => {
    install({ ...SETTINGS, llamacpp_url: AT }, DOWN)
    await open()
    await press(button('Check')!)
    await act(async () => {})
    expect(find).toHaveBeenCalledTimes(2)
    look.mockResolvedValue(SERVES)
    await press(button('Check')!)
    await act(async () => {})
    expect(start()).toBeNull()
    expect(find).toHaveBeenCalledTimes(2)
  })

  it('does not look at the machine while a llama-server serves at the saved address', async () => {
    install({ ...SETTINGS, llamacpp_url: AT }, SERVES)
    await open()
    expect(find).not.toHaveBeenCalled()
    expect(start()).toBeNull()
  })

  it('shows the plain line and no block where the server that answers is from before it could look', async () => {
    install({ ...SETTINGS, llamacpp_url: AT }, DOWN, new Error('HTTP 404'))
    await open()
    expect(start()).toBeNull()
    expect(section().textContent).toContain(`Nothing answers at ${AT}. Start llama-server (llama-server -m your-model.gguf -c 32768), then press Check.`)
  })

  it('puts the block away while another address is being typed', async () => {
    install(SETTINGS, OFF)
    await open()
    expect(start()).not.toBeNull()
    const field = host.querySelector<HTMLInputElement>('input[aria-label="llama.cpp address"]')!
    const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
    await act(async () => { set.call(field, 'http://127.0.0.1:8080'); field.dispatchEvent(new Event('input', { bubbles: true })) })
    expect(start()).toBeNull()
  })
})
