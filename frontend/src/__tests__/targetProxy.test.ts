// @vitest-environment happy-dom
import { act, createElement, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { PROXY_REFUSED, TargetForm } from '../components/TargetForm'
import { emptyScopeForm, type ScopeForm } from '../scope'

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

/** The form as a dialog holds it: the proxy is its own piece of state next to the scope form. */
function Held({ start, problem, targetId, kept }: { start: string; problem?: string; targetId?: string; kept?: boolean }) {
  const [form, setForm] = useState<ScopeForm>(emptyScopeForm)
  const [proxy, setProxy] = useState(start)
  return createElement(TargetForm, { form, problem, proxy, proxyKept: kept, targetId, onProxy: setProxy, onChange: setForm, onUnread: () => {} })
}

const field = () => host.querySelector<HTMLInputElement>('input[aria-label="Proxy for this target"]')!
const button = () => [...host.querySelectorAll('button')].find((b) => /Test/.test(b.textContent ?? ''))!
const alerts = () => [...host.querySelectorAll('[role="alert"]')].map((a) => a.textContent)

function type(el: HTMLInputElement, value: string): void {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  act(() => { set.call(el, value); el.dispatchEvent(new Event('input', { bubbles: true })) })
}

describe("a target's own proxy in the target form", () => {
  it('is tested as it stands in the field, with the target it belongs to, and says what sites see', async () => {
    const test = vi.spyOn(api, 'testProxy').mockResolvedValue({ ok: true, through: 'fr.example.net:1080', ms: 310, ip: '185.1.2.3', country: 'FR', error: '' })
    act(() => root.render(createElement(Held, { start: 'socks5://hunter@fr.example.net:1080', targetId: 't1', kept: true })))
    expect(host.textContent).toContain('a password is stored and not shown')
    await act(async () => { button().click() })
    expect(test).toHaveBeenCalledWith('socks5://hunter@fr.example.net:1080', 't1')
    expect(host.textContent).toContain('Works: sites see 185.1.2.3, France (FR). Answered in 310 ms.')
    type(field(), 'socks5://hunter@other.example.net:1080')            // another address: what was found is no longer about it
    expect(host.textContent).not.toContain('Works: sites see')
  })
  it('has nothing to test while the field is empty', () => {
    act(() => root.render(createElement(Held, { start: '' })))
    expect(button().disabled).toBe(true)
    expect(host.textContent).not.toContain('a password is stored')
    type(field(), 'http://127.0.0.1:8080')
    expect(button().disabled).toBe(false)
  })
  it('shows a refused address beside the proxy field, and a refused scope line beside the lists', () => {
    act(() => root.render(createElement(Held, { start: 'fr.example.net', problem: `${PROXY_REFUSED}: give the port of the proxy too, like 127.0.0.1:8080.` })))
    expect(alerts()).toEqual([`${PROXY_REFUSED}: give the port of the proxy too, like 127.0.0.1:8080.`])
    expect(field().closest('label')!.nextElementSibling!.getAttribute('role')).toBe('alert')
    act(() => root.render(createElement(Held, { start: '', problem: 'These scope lines cannot be read' })))
    expect(alerts()).toEqual(['These scope lines cannot be read'])
    expect(field().closest('label')!.nextElementSibling!.getAttribute('role')).not.toBe('alert')
  })
  it('says why when the test itself could not be made', async () => {
    vi.spyOn(api, 'testProxy').mockRejectedValue(new Error('proxy: give the port of the proxy too, like 127.0.0.1:8080'))
    act(() => root.render(createElement(Held, { start: 'fr.example.net' })))
    await act(async () => { button().click() })
    expect(host.textContent).toContain('proxy: give the port of the proxy too')
  })
})
