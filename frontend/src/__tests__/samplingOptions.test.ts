// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AdvancedOptions } from '../components/AdvancedOptions'
import { SAMPLING } from '../options'
import { useStore } from '../store'
import { pruneOptions } from '../tree'
import type { Chat, ModelInfo, Options, ProviderInfo } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const LOCAL: ModelInfo = { id: 'qwen3:8b', label: 'qwen3:8b', supports_images: false, context_window: 16384, efforts: [],
  options: ['temperature', 'top_p', 'max_tokens', 'context_window', ...SAMPLING, 'thinking'] }
const CLOUD: ModelInfo = { id: 'big', label: 'big', supports_images: true, context_window: 200000, efforts: [], options: ['temperature', 'max_tokens'] }
const PROVIDERS: ProviderInfo[] = [{ name: 'ollama', configured: true, models: [LOCAL] }, { name: 'abliteration', configured: true, models: [CLOUD] }]
const CHAT: Chat = { id: 'c1', target_id: null, title: 't', mode: 'pentesting', provider: 'ollama', model: 'qwen3:8b', options: {},
  active_leaf_id: null, created_at: '', updated_at: '', workdir: null, permissions: {} }
const LABELS = ['Top K', 'Min P', 'Repeat penalty', 'Repeat last N (tokens)', 'Presence penalty', 'Frequency penalty', 'Seed']

let root: Root
let host: HTMLElement
let saved: Options[]

function install(chat: Chat): void {
  saved = []
  useStore.setState({
    chat, providers: PROVIDERS, live: null,
    // What the store does with a patch, without the server: the chat has the new options at once.
    patchChat: async (patch: { options?: Options }) => {
      if (patch.options) saved.push(patch.options)
      useStore.setState((s) => ({ chat: { ...s.chat!, ...patch } as Chat }))
    },
  })
}

beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  install(CHAT)
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.restoreAllMocks()
})

async function open(): Promise<void> {
  await act(async () => { root.render(createElement(AdvancedOptions)) })
  await act(async () => { host.querySelector<HTMLButtonElement>('button[aria-expanded]')!.click() })
}
const field = (label: string) => [...host.querySelectorAll<HTMLLabelElement>('label.field')].find((l) => l.querySelector('span')?.textContent === label)
const input = (label: string) => field(label)!.querySelector<HTMLInputElement>('input')!
const shown = () => [...host.querySelectorAll('label.field > span')].map((s) => s.textContent)

async function type(el: HTMLInputElement, value: string): Promise<void> {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  await act(async () => {
    set.call(el, value)
    el.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

describe('the names of the sampling options', () => {
  it('are the seven the local servers take', () => {
    expect([...SAMPLING]).toEqual(['top_k', 'min_p', 'repeat_penalty', 'repeat_last_n', 'presence_penalty', 'frequency_penalty', 'seed'])
  })
})

describe('the sampling options of a chat', () => {
  it('are offered for a model that takes them, under a heading of their own and with what they do', async () => {
    await open()
    expect(shown().filter((label) => LABELS.includes(label!))).toEqual(LABELS)
    expect(host.querySelector('.sheet .section')?.textContent).toBe('Sampling')
    const hint = [...host.querySelectorAll('.sheet .hint')].map((h) => h.textContent).find((t) => t!.includes('Top K'))!
    expect(hint).toContain("what is not set is the server's own default")
    expect(hint).toContain('Repeat penalty above 1 (1 is none)')
    expect(hint).toContain('The same seed gives the same answer to the same request.')
  })

  it('are not offered for a model that does not take them', async () => {
    install({ ...CHAT, provider: 'abliteration', model: 'big' })
    await open()
    expect(shown()).toEqual(['Temperature', 'Max output tokens'])
    expect(host.querySelector('.sheet .section')).toBeNull()
    expect(host.querySelector('.sheet')!.textContent).not.toContain('Top K')
  })

  it('offers only those of them a model takes', async () => {
    const some = { ...LOCAL, options: ['temperature', 'top_k', 'seed'] }
    useStore.setState({ providers: [{ name: 'ollama', configured: true, models: [some] }] })
    await open()
    expect(shown()).toEqual(['Temperature', 'Top K', 'Seed'])
    expect(host.querySelector('.sheet .section')?.textContent).toBe('Sampling')
  })

  it('saves each one with the chat as it is set', async () => {
    await open()
    await type(input('Top K'), '20')
    expect(saved.pop()).toEqual({ top_k: 20 })
    await type(input('Min P'), '0.1')
    await type(input('Repeat penalty'), '1.15')
    await type(input('Repeat last N (tokens)'), '128')
    await type(input('Presence penalty'), '0.5')
    await type(input('Frequency penalty'), '-0.3')
    await type(input('Seed'), '42')
    expect(saved.pop()).toEqual({ top_k: 20, min_p: 0.1, repeat_penalty: 1.15, repeat_last_n: 128, presence_penalty: 0.5, frequency_penalty: -0.3, seed: 42 })
    expect(useStore.getState().chat!.options.repeat_penalty).toBe(1.15)
    expect(host.querySelector('button[aria-expanded]')!.textContent).toBe('Options · 7')
  })

  it('keeps a seed of 0, and takes a Top K or a look-back below 1 as not set', async () => {
    await open()
    await type(input('Seed'), '0')
    expect(saved.pop()).toEqual({ seed: 0 })
    await type(input('Seed'), '-1')
    expect(saved.pop()).toEqual({})
    await type(input('Top K'), '0')
    expect(saved.pop()).toEqual({})
    await type(input('Top K'), '40')
    await type(input('Repeat last N (tokens)'), '0')
    expect(saved.pop()).toEqual({ top_k: 40 })
    await type(input('Top K'), '')
    expect(saved.pop()).toEqual({})
    expect(input('Seed').min).toBe('0')
    expect(input('Top K').min).toBe('1')
  })

  it('takes a seed back when its field is emptied, and does not make it 0', async () => {
    await open()
    await type(input('Seed'), '7')
    expect(saved.pop()).toEqual({ seed: 7 })
    await type(input('Seed'), '')
    expect(saved.pop()).toEqual({})
  })

  it('shows what is set when the options are opened again, and takes one back with its reset', async () => {
    install({ ...CHAT, options: { repeat_penalty: 1.3, presence_penalty: 0.5, top_k: 20 } })
    await open()
    expect(input('Repeat penalty').value).toBe('1.3')
    expect(field('Repeat penalty')!.querySelector('output')!.textContent).toBe('1.3')
    expect(input('Top K').value).toBe('20')
    await act(async () => { field('Repeat penalty')!.querySelector<HTMLButtonElement>('button')!.click() })
    expect(saved.pop()).toEqual({ presence_penalty: 0.5, top_k: 20 })
    expect(field('Repeat penalty')!.querySelector('output')!.textContent).toBe('—')
    expect(host.querySelector('button[aria-expanded]')!.textContent).toBe('Options · 2')
  })

  it('rests a slider that is not set at the value that changes nothing, within the range a server takes', async () => {
    await open()
    expect([input('Repeat penalty').value, input('Repeat penalty').min, input('Repeat penalty').max]).toEqual(['1', '0.5', '2'])
    expect([input('Presence penalty').value, input('Presence penalty').min, input('Presence penalty').max]).toEqual(['0', '-2', '2'])
    expect([input('Frequency penalty').value, input('Frequency penalty').min, input('Frequency penalty').max]).toEqual(['0', '-2', '2'])
    expect([input('Min P').value, input('Min P').min, input('Min P').max]).toEqual(['0', '0', '1'])
    expect(input('Temperature').value).toBe('0')                 // the sliders that were there rest where they did
    expect(['Min P', 'Repeat penalty', 'Presence penalty', 'Frequency penalty'].map((label) => input(label).step)).toEqual(['0.01', '0.05', '0.1', '0.1'])
    expect(field('Repeat penalty')!.querySelector('output')!.textContent).toBe('—')
    expect(field('Repeat penalty')!.querySelector<HTMLButtonElement>('button')!.disabled).toBe(true)
  })

  it('drops them when the chat moves to a model that does not take them', () => {
    const set: Options = { temperature: 0.2, top_k: 20, repeat_penalty: 1.2, seed: 0 }
    expect(pruneOptions(set, CLOUD.options)).toEqual({ temperature: 0.2 })
    expect(pruneOptions(set, LOCAL.options)).toEqual(set)
  })
})
