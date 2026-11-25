// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { ChatKbBar } from '../components/ChatKbBar'
import { useStore } from '../store'
import type { Chat, ChatKb, Collection } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const col = (id: string, name: string): Collection => ({ id, name, embedder: 'ollama:bge-m3', dimension: 1024, created_at: '2026-11-22T00:00:00Z' })
const COLLECTIONS = [col('rag', 'RAG'), col('notes', 'Notes')]
// What the server answers for a chat nobody chose collections for: it reads the ones named for every chat.
const kb = (ids: string[]): ChatKb => ({ chat_id: 'c1', collection_ids: ids, always_retrieve: true, follows: true })

let host: HTMLDivElement
let root: Root
let named: string[]
let put: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  named = []
  vi.spyOn(api, 'getChatKb').mockImplementation(async () => kb(named))
  vi.spyOn(api, 'listCollections').mockImplementation(async () => COLLECTIONS)
  vi.spyOn(api, 'getNewChatKb').mockImplementation(async () => ({ collection_ids: named }))
  put = vi.spyOn(api, 'putNewChatKb').mockImplementation(async (ids: string[]) => { named = ids; return { collection_ids: ids } })
  useStore.setState({ chat: { id: 'c1' } as Chat, live: null })
})
afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks(); useStore.setState({ chat: null, live: null }) })

const flush = async () => { for (let i = 0; i < 4; i++) await act(async () => { await Promise.resolve() }) }
const open = async () => {
  await act(async () => { root.render(createElement(ChatKbBar)) }); await flush()
  await act(async () => { [...host.querySelectorAll('button')].find((b) => b.textContent === '+ Knowledge')!.click() }); await flush()
}
const every = (name: string) => host.querySelector<HTMLInputElement>(`input[aria-label="Attach ${name} to every chat"]`)!
const chips = () => [...host.querySelectorAll('.kb-bar > .chip')].map((c) => c.textContent)
const warning = () => host.querySelector('.kb-bar .warn-line')?.textContent ?? null

describe('the collections every chat reads, in the knowledge control of a chat', () => {
  it('each collection can be named for every chat, beside attaching it to this one', async () => {
    await open()
    expect(every('RAG').checked).toBe(false)
    expect(every('Notes').checked).toBe(false)
    await act(async () => { every('RAG').click() }); await flush()
    expect(put).toHaveBeenCalledWith(['rag'])
    expect(every('RAG').checked).toBe(true)
    expect(every('Notes').checked).toBe(false)
  })

  it('shows what was named before, and takes a collection out again', async () => {
    named = ['rag', 'notes']
    await open()
    expect([every('RAG').checked, every('Notes').checked]).toEqual([true, true])
    await act(async () => { every('RAG').click() }); await flush()
    expect(put).toHaveBeenCalledWith(['notes'])
    expect([every('RAG').checked, every('Notes').checked]).toEqual([false, true])
  })

  it('is read by the chat that is open at once, and nothing is written as this chat\'s own choice', async () => {
    const attach = vi.spyOn(api, 'putChatKb')
    await open()
    expect(chips()).toEqual([])
    expect(warning()).toContain('This chat reads no collection')
    await act(async () => { every('RAG').click() }); await flush()
    expect(attach).not.toHaveBeenCalled()             // the box in front of the name is this chat's own choice
    expect(chips()).toEqual(['RAG · every chat✕'])
    expect(warning()).toBeNull()
    await act(async () => { every('RAG').click() }); await flush()
    expect(chips()).toEqual([])
    expect(warning()).toContain('This chat reads no collection')
  })

  it('does not mark the collections of a chat that has its own', async () => {
    vi.spyOn(api, 'getChatKb').mockImplementation(async () => ({ chat_id: 'c1', collection_ids: ['notes'], always_retrieve: true, follows: false }))
    await open()
    expect(chips()).toEqual(['Notes✕'])
    await act(async () => { every('RAG').click() }); await flush()
    expect(chips()).toEqual(['Notes✕'])
  })

  it('says what went wrong when it cannot be saved, and stays as it was', async () => {
    put.mockImplementation(async () => { throw new Error('collection not found') })
    await open()
    await act(async () => { every('RAG').click() }); await flush()
    expect(every('RAG').checked).toBe(false)
    expect(useStore.getState().error).toBe('collection not found')
  })
})
