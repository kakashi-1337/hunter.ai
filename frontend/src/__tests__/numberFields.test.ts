// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { AdvancedOptions } from '../components/AdvancedOptions'
import { typedNumber } from '../options'
import { useStore } from '../store'
import type { Chat, ModelInfo, Options, ProviderInfo } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const LOCAL: ModelInfo = { id: 'qwen3:8b', label: 'qwen3:8b', supports_images: false, context_window: 16384, efforts: [],
  options: ['temperature', 'max_tokens', 'context_window', 'top_k', 'seed'] }
const PROVIDERS: ProviderInfo[] = [{ name: 'ollama', configured: true, models: [LOCAL] }]
const CHAT: Chat = { id: 'c1', target_id: null, title: 't', mode: 'pentesting', provider: 'ollama', model: 'qwen3:8b', options: {},
  active_leaf_id: null, created_at: '', updated_at: '', workdir: null, permissions: {} }
const TOKENS = 'Max output tokens'

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
})

async function open(): Promise<void> {
  await act(async () => { root.render(createElement(AdvancedOptions)) })
  await act(async () => { host.querySelector<HTMLButtonElement>('button[aria-expanded]')!.click() })
}
const isOpen = () => host.querySelector('.sheet') !== null
const input = (label: string) => [...host.querySelectorAll<HTMLLabelElement>('label.field')]
  .find((l) => l.querySelector('span')?.textContent === label)!.querySelector<HTMLInputElement>('input')!

/** The field has the focus and holds `value`, as after the keys that make it. */
async function type(el: HTMLInputElement, value: string): Promise<void> {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  await act(async () => {
    el.focus()
    set.call(el, value)
    el.dispatchEvent(new Event('input', { bubbles: true }))
  })
}
const press = (el: HTMLElement, key: string) => act(async () => { el.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true })) })
const leave = (el: HTMLElement) => act(async () => { el.blur() })
const clickElsewhere = () => act(async () => { document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })) })

describe('what a number field of the options takes', () => {
  it('is a whole number of the least one or more', () => {
    expect(typedNumber('16000')).toBe(16000)
    expect(typedNumber(' 512 ')).toBe(512)
    expect(typedNumber('1')).toBe(1)
    expect(typedNumber('007')).toBe(7)
    expect(typedNumber('0', 0)).toBe(0)
    expect(typedNumber('5', 5)).toBe(5)
  })

  it('is nothing for an empty field and for what is no such number', () => {
    for (const text of ['', '   ', '0', '-1', '+5', '16k', '1.5', '1e3', '0x10', 'abc', '１６', '9007199254740993']) {
      expect(typedNumber(text), text).toBeUndefined()
    }
    expect(typedNumber('-1', 0)).toBeUndefined()
    expect(typedNumber('4', 5)).toBeUndefined()
  })

  it('reads the commas or spaces between thousands, and nothing else between digits', () => {
    expect(typedNumber('16,000')).toBe(16000)
    expect(typedNumber('1,000,000')).toBe(1000000)
    expect(typedNumber('1 000 000')).toBe(1000000)
    for (const text of ['1,6', '16,5', '16,0000', '1234,567', '1234 567', ',000', '16,', '12 34', '1,,000', '1.000']) expect(typedNumber(text), text).toBeUndefined()
  })
})

describe('a number field of the options of a chat', () => {
  it('saves nothing while a number is typed, and saves the number once with Enter', async () => {
    await open()
    for (const soFar of ['1', '16', '160', '1600', '16000']) await type(input(TOKENS), soFar)
    expect(saved).toEqual([])                         // "1" was a limit of one token for as long as it stood there
    expect(input(TOKENS).value).toBe('16000')
    await press(input(TOKENS), 'Enter')
    expect(saved).toEqual([{ max_tokens: 16000 }])
    expect(useStore.getState().chat!.options).toEqual({ max_tokens: 16000 })
    expect(input(TOKENS).value).toBe('16000')
    expect(isOpen()).toBe(true)
    await leave(input(TOKENS))                        // done with Enter already: leaving it then saves nothing again
    expect(saved).toEqual([{ max_tokens: 16000 }])
  })

  it('saves the number when the field is left', async () => {
    await open()
    await type(input('Top K'), '4')
    await type(input('Top K'), '40')
    expect(saved).toEqual([])
    await leave(input('Top K'))
    expect(saved).toEqual([{ top_k: 40 }])
    expect(input('Top K').value).toBe('40')
  })

  it('saves the number when the options are closed with a click elsewhere', async () => {
    await open()
    await type(input(TOKENS), '2048')
    await clickElsewhere()
    expect(isOpen()).toBe(false)
    expect(saved).toEqual([{ max_tokens: 2048 }])
  })

  it('stays open with a click inside the options, and what is typed waits', async () => {
    await open()
    await type(input(TOKENS), '40')
    await act(async () => { input('Top K').dispatchEvent(new MouseEvent('mousedown', { bubbles: true })) })
    expect(isOpen()).toBe(true)
    expect(saved).toEqual([])                         // a browser takes the focus off the field next, and that saves it
    expect(input(TOKENS).value).toBe('40')
  })

  it('leaves the focus where it is when that is outside the options that close', async () => {
    const other = document.createElement('input')
    document.body.appendChild(other)
    await open()
    await act(async () => { other.focus() })
    await act(async () => { other.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })) })    // a click into the field that has the focus
    expect(isOpen()).toBe(false)
    expect(document.activeElement).toBe(other)
    other.remove()
  })

  it('drops what was typed when the options are closed with Escape', async () => {
    install({ ...CHAT, options: { max_tokens: 512 } })
    await open()
    await type(input(TOKENS), '5')
    await press(input(TOKENS), 'Escape')
    expect(isOpen()).toBe(false)
    expect(saved).toEqual([])
    await act(async () => { host.querySelector<HTMLButtonElement>('button[aria-expanded]')!.click() })
    expect(input(TOKENS).value).toBe('512')
  })

  it('is a field for text, which has no steps: neither the wheel nor an arrow key sets it', async () => {
    await open()
    for (const label of [TOKENS, 'Context window (tokens)', 'Top K', 'Seed']) {
      const el = input(label)
      expect([label, el.type, el.inputMode, el.hasAttribute('min'), el.hasAttribute('step')]).toEqual([label, 'text', 'numeric', false, false])
    }
    await act(async () => { input(TOKENS).focus() })
    await press(input(TOKENS), 'ArrowUp')
    await act(async () => { input(TOKENS).dispatchEvent(new WheelEvent('wheel', { deltaY: -120, bubbles: true })) })
    await leave(input(TOKENS))
    expect(saved).toEqual([])
    expect(input(TOKENS).value).toBe('')
  })

  it('takes the option back when the field is emptied', async () => {
    install({ ...CHAT, options: { max_tokens: 1, top_k: 20 } })
    await open()
    expect(input(TOKENS).value).toBe('1')
    await type(input(TOKENS), '')
    expect(saved).toEqual([])
    await press(input(TOKENS), 'Enter')
    expect(saved).toEqual([{ top_k: 20 }])
    expect(input(TOKENS).value).toBe('')
    expect(input(TOKENS).placeholder).toBe('model default')
    expect(host.querySelector('button[aria-expanded]')!.textContent).toBe('Options · 1')
  })

  it('saves nothing when the field is left as it was', async () => {
    install({ ...CHAT, options: { max_tokens: 512 } })
    await open()
    await act(async () => { input(TOKENS).focus() })
    await leave(input(TOKENS))
    await type(input(TOKENS), '51')
    await type(input(TOKENS), '512')
    await leave(input(TOKENS))
    await type(input(TOKENS), ' 512 ')
    await press(input(TOKENS), 'Enter')
    expect(saved).toEqual([])
    expect(input(TOKENS).value).toBe('512')
  })

  it('takes what is not a whole number as not set, and shows that', async () => {
    await open()
    await type(input(TOKENS), '16k')
    await press(input(TOKENS), 'Enter')
    expect(saved).toEqual([])                         // it was not set before either
    expect(input(TOKENS).value).toBe('')
    install({ ...CHAT, options: { max_tokens: 512, seed: 3 } })
    await act(async () => { root.render(createElement(AdvancedOptions)) })
    await type(input(TOKENS), '0')
    await leave(input(TOKENS))
    expect(saved).toEqual([{ seed: 3 }])
    expect(input(TOKENS).value).toBe('')
    await type(input('Seed'), '0')                    // a seed of 0 is one
    await leave(input('Seed'))
    expect(saved.pop()).toEqual({ seed: 0 })
    await type(input('Seed'), '-1')
    await leave(input('Seed'))
    expect(saved.pop()).toEqual({})
  })

  it('reads a number that is written with commas', async () => {
    await open()
    await type(input('Context window (tokens)'), '32,768')
    await press(input('Context window (tokens)'), 'Enter')
    expect(saved).toEqual([{ context_window: 32768 }])
    expect(input('Context window (tokens)').value).toBe('32768')
  })

  it('keeps what is being typed when the chat gets other options meanwhile', async () => {
    await open()
    await type(input(TOKENS), '40')
    await act(async () => { useStore.setState((s) => ({ chat: { ...s.chat!, options: { context_window: 32768 } } })) })    // a run raised the window
    expect(input(TOKENS).value).toBe('40')
    expect(input('Context window (tokens)').value).toBe('32768')
    await type(input(TOKENS), '4096')
    await press(input(TOKENS), 'Enter')
    expect(saved).toEqual([{ context_window: 32768, max_tokens: 4096 }])
  })
})
