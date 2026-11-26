// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { AdvancedOptions } from '../components/AdvancedOptions'
import { useStore } from '../store'
import { optionsNote } from '../tree'
import type { Chat, ModelInfo, ProviderInfo } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const AT = 'http://127.0.0.1:8000'
const model = (id: string, options: string[]): ModelInfo => ({ id, label: id, supports_images: false, context_window: 16384, efforts: [], options })
const LOCAL = model('qwen3:8b', ['temperature', 'top_p'])
const BARE = model('plain', [])
const OLLAMA: ProviderInfo = { name: 'ollama', configured: true, models: [LOCAL, BARE] }
const DOWN: ProviderInfo = { name: 'colibri', configured: true, models: [], note: `nothing answers at ${AT}` }
const UNLISTED = 'so the options it takes are not known. They show again when the model is back in the list; or pick another model for this chat.'

describe('what stands in place of the options of a chat', () => {
  it('is nothing for a model that has options', () => {
    expect(optionsNote('ollama', 'qwen3:8b', [OLLAMA, DOWN])).toBeNull()
  })

  it('says that a listed model takes none', () => {
    expect(optionsNote('ollama', 'plain', [OLLAMA])).toBe('This model has no adjustable options.')
  })

  it('says that a model nobody serves now is not offered, with what its provider says of itself', () => {
    expect(optionsNote('colibri', 'qwen3.8-27b-colibri', [OLLAMA, DOWN]))
      .toBe(`colibri does not offer qwen3.8-27b-colibri now (nothing answers at ${AT}), ${UNLISTED}`)
  })

  it('says so of a provider that is not set up, and of one that says nothing of itself', () => {
    expect(optionsNote('colibri', 'big', [{ name: 'colibri', configured: false, models: [] }]))
      .toBe(`colibri does not offer big now (not configured), ${UNLISTED}`)
    expect(optionsNote('ollama', 'gone:1b', [{ name: 'ollama', configured: true, models: [] }]))
      .toBe(`ollama does not offer gone:1b now (no models available), ${UNLISTED}`)
  })

  it('gives no reason where the provider offers other models, or is not in the list', () => {
    expect(optionsNote('ollama', 'gone:1b', [OLLAMA])).toBe(`ollama does not offer gone:1b now, ${UNLISTED}`)
    expect(optionsNote('old', 'big', [OLLAMA])).toBe(`old does not offer big now, ${UNLISTED}`)
    expect(optionsNote('old', 'big', [])).toBe(`old does not offer big now, ${UNLISTED}`)
  })

  it('does not take a model of that name at another provider for it', () => {
    expect(optionsNote('colibri', 'qwen3:8b', [OLLAMA, DOWN])).toBe(`colibri does not offer qwen3:8b now (nothing answers at ${AT}), ${UNLISTED}`)
  })
})

describe('the options sheet of a chat', () => {
  const CHAT: Chat = { id: 'c1', target_id: null, title: 't', mode: 'pentesting', provider: 'ollama', model: 'qwen3:8b', options: {},
    active_leaf_id: null, created_at: '', updated_at: '', workdir: null, permissions: {} }
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
  })

  async function open(chat: Chat): Promise<string[]> {
    useStore.setState({ chat, providers: [OLLAMA, DOWN], live: null })
    await act(async () => { root.render(createElement(AdvancedOptions)) })
    await act(async () => { host.querySelector<HTMLButtonElement>('button[aria-expanded]')!.click() })
    return [...host.querySelectorAll('.sheet .hint')].map((h) => h.textContent ?? '')
  }

  it('says of a chat on a model nobody serves now that its options are not known, and nothing else', async () => {
    const hints = await open({ ...CHAT, provider: 'colibri', model: 'qwen3.8-27b-colibri' })
    expect(hints).toEqual([`colibri does not offer qwen3.8-27b-colibri now (nothing answers at ${AT}), ${UNLISTED}`])
    expect(host.querySelectorAll('.sheet label.field')).toHaveLength(0)
  })

  it('says of a listed model without options that it has none, and nothing else', async () => {
    expect(await open({ ...CHAT, model: 'plain' })).toEqual(['This model has no adjustable options.'])
  })

  it('shows the options of a model that has some, and that only those it takes are shown', async () => {
    const hints = await open(CHAT)
    expect([...host.querySelectorAll('.sheet label.field > span')].map((s) => s.textContent)).toEqual(['Temperature', 'Top P'])
    expect(hints).toEqual(['Only options this model accepts are shown. Saved for this chat.'])
  })
})
