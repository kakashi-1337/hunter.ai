// @vitest-environment happy-dom
import { act, createElement, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { TargetForm } from '../components/TargetForm'
import { emptyScopeForm, type ScopeForm } from '../scope'
import type { ScopeAnswer } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

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
  vi.restoreAllMocks()
})

const saved = vi.fn()

/** The form as a dialog holds it, with two lists already in it. */
function Held({ inScope = '*.example.com', outScope = 'blog.example.com' }: { inScope?: string; outScope?: string }) {
  const [form, setForm] = useState<ScopeForm>(() => ({ ...emptyScopeForm(), name: 'Example', inScope, outScope }))
  const [proxy, setProxy] = useState('')
  return createElement(TargetForm, { form, proxy, onProxy: setProxy, onChange: setForm, onUnread: () => {}, onEnter: saved })
}

const answer = (over: Partial<ScopeAnswer> = {}): ScopeAnswer => ({
  host: 'example.com', port: null, path: null, verdict: 'not_in', line: null, command: null,
  says: 'example.com is not on the In scope list, so a call that names it is asked about.',
  hints: ['The In scope line *.example.com covers the names under example.com, not example.com itself. If the program covers example.com too, add it on a line of its own.'],
  unreadable: [], ...over,
})

const field = () => host.querySelector<HTMLInputElement>('input[aria-label="Host, address or URL to check"]')!
const button = () => [...host.querySelectorAll('button')].find((b) => /^Check/.test(b.textContent ?? ''))!
const list = (label: string) => [...host.querySelectorAll('label.field')].find((l) => l.querySelector('span')?.textContent?.startsWith(label))!.querySelector('textarea')!
const shown = () => host.querySelector('.scope-answer')

function type(el: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
  const set = Object.getOwnPropertyDescriptor(proto, 'value')!.set!
  act(() => { set.call(el, value); el.dispatchEvent(new Event('input', { bubbles: true })) })
}
const enter = (el: HTMLElement) => act(async () => { el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })) })

describe('checking one host against the scope lists of the target form', () => {
  it('asks the server with the two lists and the host, and hands back its answer', async () => {
    const fetched = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(answer()), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    expect(await api.checkScope({ in: ['*.example.com'], out: ['blog.example.com'] }, 'example.com')).toEqual(answer())
    expect(fetched).toHaveBeenCalledWith('/api/targets/scope-check', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope: { in: ['*.example.com'], out: ['blog.example.com'] }, ask: 'example.com' }),
    })
  })
  it('has nothing to check while the field is empty', () => {
    act(() => root.render(createElement(Held, {})))
    expect(button().disabled).toBe(true)
    expect(shown()).toBeNull()
    expect(host.textContent).toContain('No name is looked up')
    type(field(), '   ')
    expect(button().disabled).toBe(true)
    type(field(), 'example.com')
    expect(button().disabled).toBe(false)
  })
  it('sends the lists as they stand in the form and shows the answer, its hints, and the lines that cannot be read', async () => {
    const check = vi.spyOn(api, 'checkScope').mockResolvedValue(answer({ unreadable: [{ side: 'out', line: 'old.example.com (legacy)', why: 'one host, address or network on a line, with nothing after it' }] }))
    act(() => root.render(createElement(Held, { inScope: ' *.example.com \n\napi.example.com/v2', outScope: 'blog.example.com\nold.example.com (legacy)' })))
    type(field(), 'example.com')
    await act(async () => { button().click() })
    expect(check).toHaveBeenCalledWith({ in: ['*.example.com', 'api.example.com/v2'], out: ['blog.example.com', 'old.example.com (legacy)'] }, 'example.com')
    expect(shown()!.className).toContain('warn')
    expect([...shown()!.querySelectorAll('.says')].map((x) => [x.className, x.textContent])).toEqual([
      ['says warn', 'example.com is not on the In scope list, so a call that names it is asked about.']])
    expect([...shown()!.querySelectorAll('.hint')].map((h) => h.textContent)).toEqual([answer().hints[0]])
    expect(shown()!.textContent).toContain('Out of scope: old.example.com (legacy) (one host, address or network on a line, with nothing after it)')
  })
  it('marks an answer that lets a call through differently from one that asks', async () => {
    vi.spyOn(api, 'checkScope').mockResolvedValue(answer({ verdict: 'in', line: '*.example.com', says: 'shop.example.com is in scope: the In scope line *.example.com covers it.', hints: [] }))
    act(() => root.render(createElement(Held, {})))
    type(field(), 'shop.example.com')
    await act(async () => { button().click() })
    expect(shown()!.className).toBe('scope-answer ok')
    expect([...shown()!.querySelectorAll('.says')].map((x) => x.className)).toEqual(['says ok'])
    expect(shown()!.querySelectorAll('.hint').length).toBe(0)
    expect(shown()!.querySelector('.scope-problem')).toBeNull()
  })
  it('says in a line of its own, marked for itself, what a command gets where that is another answer', async () => {
    vi.spyOn(api, 'checkScope').mockResolvedValue(answer({
      verdict: 'in', line: '*.example.com', says: 'www.example.com is in scope: the In scope line *.example.com covers it.',
      command: { verdict: 'out', says: 'In a command, www.example.com is out of scope: the Out of scope line www.example.com/blog names it.' },
      hints: ['A command is checked without the path of an address it names, so a line with a path counts for the whole host there.'],
    }))
    act(() => root.render(createElement(Held, {})))
    type(field(), 'https://www.example.com/shop')
    await act(async () => { button().click() })
    expect([...shown()!.querySelectorAll('.says')].map((x) => [x.className, x.textContent])).toEqual([
      ['says ok', 'www.example.com is in scope: the In scope line *.example.com covers it.'],
      ['says warn', 'In a command, www.example.com is out of scope: the Out of scope line www.example.com/blog names it.']])
    expect(shown()!.className).toBe('scope-answer warn')           // the box is marked by the answer that asks
    expect(shown()!.querySelectorAll('.hint').length).toBe(1)
  })
  it('checks on Enter and does not save the form with it', async () => {
    const check = vi.spyOn(api, 'checkScope').mockResolvedValue(answer())
    saved.mockClear()
    act(() => root.render(createElement(Held, {})))
    type(field(), 'example.com')
    await act(async () => { field().dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true, cancelable: true })) })
    expect(check).not.toHaveBeenCalled()                           // typing is not asking
    await enter(field())
    expect(check).toHaveBeenCalledTimes(1)
    expect(saved).not.toHaveBeenCalled()
    expect(shown()).not.toBeNull()
  })
  it('takes an answer away once a list or the host is changed: it was about what stood there before', async () => {
    vi.spyOn(api, 'checkScope').mockResolvedValue(answer())
    act(() => root.render(createElement(Held, {})))
    type(field(), 'example.com')
    await act(async () => { button().click() })
    expect(shown()).not.toBeNull()
    type(list('In scope'), '*.example.com\nexample.com')
    expect(shown()).toBeNull()
    type(list('In scope'), '*.example.com')                       // the lists as they were: the answer holds again
    expect(shown()).not.toBeNull()
    type(list('Out of scope'), 'blog.example.com\nexample.com')
    expect(shown()).toBeNull()
    type(list('Out of scope'), 'blog.example.com')
    type(field(), 'example.org')
    expect(shown()).toBeNull()
  })
  it('says what to type when the text names no host, in the words of the server', async () => {
    vi.spyOn(api, 'checkScope').mockRejectedValue(new api.ApiError(422, 'Type one host, address or URL, like shop.example.com or https://shop.example.com/login.'))
    act(() => root.render(createElement(Held, {})))
    type(field(), 'two words')
    await act(async () => { button().click() })
    expect(shown()).toBeNull()
    expect(host.querySelector('.scope-check [role="status"]')!.textContent).toBe('Type one host, address or URL, like shop.example.com or https://shop.example.com/login.')
    type(field(), 'two')                                            // another text: what was said is no longer about it
    expect(host.querySelector('.scope-check [role="status"]')!.textContent).toContain('No name is looked up')
  })
  it('does not show an answer that arrives after the question was changed', async () => {
    let settle: (a: ScopeAnswer) => void = () => {}
    vi.spyOn(api, 'checkScope').mockImplementation(() => new Promise<ScopeAnswer>((resolve) => { settle = resolve }))
    act(() => root.render(createElement(Held, {})))
    type(field(), 'example.com')
    await act(async () => { button().click() })
    expect(button().textContent).toBe('Checking…')
    expect(button().disabled).toBe(true)
    type(field(), 'example.org')
    await act(async () => { settle(answer()) })
    expect(shown()).toBeNull()
    expect(button().textContent).toBe('Check')
  })
})
