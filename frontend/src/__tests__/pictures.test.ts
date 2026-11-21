// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { Composer } from '../components/Composer'
import { Attachments } from '../components/MessageItem'
import { useStore } from '../store'
import type { Block, Upload } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let host: HTMLDivElement
let root: Root
beforeEach(() => { host = document.createElement('div'); document.body.append(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks(); useStore.getState().setError(null) })

const upload = (extra: Partial<Upload> = {}): Upload => ({
  id: 'u1', chat_id: null, filename: 'shot.png', mime: 'image/png', size: 400, sha256: 'a'.repeat(64), created_at: '2026-11-21T00:00:00Z', ...extra,
})

/** Pastes files into the message box, as the clipboard of a browser hands them over. */
const paste = async (files: File[]) => {
  const event = new Event('paste', { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'clipboardData', { value: { files } })
  await act(async () => { host.querySelector('textarea')!.dispatchEvent(event) })
  await act(async () => { await Promise.resolve() })
}
const chips = () => [...host.querySelectorAll('.attachments .chip')]

describe('a picture that cannot be sent to a model', () => {
  const PROBLEM = 'this picture will not be sent to a model: it cannot be decoded (the file is damaged or cut short)'

  it('is said to be that when it is attached, by its name, and its chip is marked', async () => {
    vi.spyOn(api, 'upload').mockResolvedValue(upload({ problem: PROBLEM }))
    act(() => { root.render(createElement(Composer)) })
    await paste([new File(['x'], 'shot.png', { type: 'image/png' })])
    expect(useStore.getState().error).toBe(`shot.png: ${PROBLEM}`)
    expect(chips()).toHaveLength(1)                          // it is attached all the same: the owner takes it off, or sends it
    expect(chips()[0].textContent).toContain('shot.png')
    expect(chips()[0].textContent).toContain('⚠')
    expect(chips()[0].getAttribute('title')).toBe(PROBLEM)
  })

  it('leaves a picture that is in order as it was', async () => {
    vi.spyOn(api, 'upload').mockResolvedValue(upload({ problem: '' }))
    act(() => { root.render(createElement(Composer)) })
    await paste([new File(['x'], 'shot.png', { type: 'image/png' })])
    expect(useStore.getState().error).toBeNull()
    expect(chips()[0].textContent).toContain('🖼')
    expect(chips()[0].textContent).not.toContain('⚠')
    expect(chips()[0].getAttribute('title')).toBeNull()
  })
})

describe('a picture in a message', () => {
  const blocks: Block[] = [{ type: 'image', upload_id: 'u1', mime: 'image/png' }, { type: 'image', upload_id: 'u2', mime: 'image/png' }]

  it('is shown as a thumbnail that opens the picture', () => {
    act(() => { root.render(createElement(Attachments, { blocks })) })
    expect([...host.querySelectorAll('img')].map((i) => i.getAttribute('src'))).toEqual(['/api/uploads/u1', '/api/uploads/u2'])
    expect(host.textContent).toBe('')
  })

  it('is named in words when the browser cannot show it, where only the words "attached image" stood', () => {
    act(() => { root.render(createElement(Attachments, { blocks })) })
    act(() => { host.querySelector('img[src="/api/uploads/u1"]')!.dispatchEvent(new Event('error')) })
    expect([...host.querySelectorAll('img')].map((i) => i.getAttribute('src'))).toEqual(['/api/uploads/u2'])     // the other one stays
    const said = host.querySelector('.attachments .chip')!
    expect(said.textContent).toContain('A picture that cannot be shown')
    expect(said.getAttribute('title')).toContain('damaged or cut short')
    expect(said.querySelector('a')!.getAttribute('href')).toBe('/api/uploads/u1')                               // the file can still be opened
  })
})
