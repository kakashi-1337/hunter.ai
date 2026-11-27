// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import * as api from '../api'
import { MachineStatus } from '../components/MachineStatus'
import type { MonitorReading, SystemStatus } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const GIB = 1024 ** 3
const QUIET: SystemStatus = { cpu_temp: 55, cpu_limit: 90, local_models: [], restart_needed: false, stopped: [], page: null }
const CARD = { name: 'NVIDIA GeForce RTX 5080 Laptop GPU', load: 32, temp: 50, limit: 85, power: 31.2, used: 10.2 * GIB, total: 15.9 * GIB,
  holders: [{ name: 'coli', bytes: 7.68 * GIB }] }
const READING: MonitorReading = {
  cpu: { load: 48.2, temp: 61, limit: 90 },
  ram: { used: 24.9 * GIB, total: 30.2 * GIB, holders: [{ name: 'coli', bytes: 7.5 * GIB }, { name: 'ollama', bytes: 4 * GIB }] },
  cards: [CARD],
}

let root: Root
let host: HTMLElement
let asked: MockInstance<typeof api.systemMonitor>

beforeEach(() => {
  vi.useFakeTimers()
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  vi.spyOn(api, 'systemStatus').mockResolvedValue(QUIET)
  asked = vi.spyOn(api, 'systemMonitor').mockResolvedValue(READING)
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

const render = async () => { await act(async () => { root.render(createElement(MachineStatus)) }) }
const toggle = () => [...host.querySelectorAll<HTMLButtonElement>('button')].find((b) => b.textContent === 'Monitor')!
const panel = () => host.querySelector<HTMLElement>('section[aria-label="Live monitor"]')
const click = async (b: HTMLElement) => { await act(async () => { b.click() }) }
const later = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const rowOf = (label: string) => [...host.querySelectorAll<HTMLElement>('.meter-row')].find((r) => r.querySelector('.meter-head span')?.textContent === label)!
const figure = (label: string) => rowOf(label).querySelector('output')!.textContent

describe('the live monitor', () => {
  it('is closed until it is asked for, and asks the machine nothing while it is closed', async () => {
    await render()
    await later(30_000)
    expect(panel()).toBeNull()
    expect(asked).not.toHaveBeenCalled()
    expect(toggle().getAttribute('aria-expanded')).toBe('false')
  })

  it('shows the reading when it is opened: a name, a figure and a bar for each row', async () => {
    await render()
    await click(toggle())
    expect(asked).toHaveBeenCalledTimes(1)
    expect(toggle().getAttribute('aria-expanded')).toBe('true')
    expect([...panel()!.querySelectorAll('.section')].map((s) => s.textContent)).toEqual(['This machine', 'NVIDIA GeForce RTX 5080 Laptop GPU'])
    expect(figure('RAM')).toBe('24.9 / 30.2 GiB')
    const bar = rowOf('RAM').querySelector('[role="meter"]')!
    expect(['aria-label', 'aria-valuemin', 'aria-valuemax', 'aria-valuenow', 'aria-valuetext'].map((a) => bar.getAttribute(a))).toEqual(['RAM', '0', '100', '82', '24.9 / 30.2 GiB'])
    expect(bar.querySelector<HTMLElement>('span')!.style.width).toBe('82.5%')
    expect([...rowOf('RAM').querySelectorAll('.meter-parts li')].map((li) => [...li.children].map((c) => c.textContent))).toEqual(
      [['coli', '7.50 GiB'], ['ollama', '4.00 GiB'], ['other', '13.4 GiB'], ['free', '5.30 GiB']])
    expect(figure('Power')).toBe('31 W')
    expect(rowOf('Power').querySelector('[role="meter"]')).toBeNull()
    expect(rowOf('CPU').querySelector('.meter-parts')).toBeNull()
    expect(rowOf('RAM').getAttribute('title')).toContain('Memory in use')
  })

  it('asks again every three seconds while it is open, and no more once it is closed', async () => {
    await render()
    await click(toggle())
    await later(2900)
    expect(asked).toHaveBeenCalledTimes(1)
    asked.mockResolvedValue({ ...READING, cpu: { load: 7, temp: 61, limit: 90 } })
    await later(100)
    expect(asked).toHaveBeenCalledTimes(2)
    expect(figure('CPU')).toBe('7%')
    await later(3000)
    expect(asked).toHaveBeenCalledTimes(3)
    await click(panel()!.querySelector<HTMLButtonElement>('button[aria-label="Close the live monitor"]')!)
    expect(panel()).toBeNull()
    expect(toggle().getAttribute('aria-expanded')).toBe('false')
    await later(30_000)
    expect(asked).toHaveBeenCalledTimes(3)
    await click(toggle())                          // opened again: it asks at once
    expect(asked).toHaveBeenCalledTimes(4)
    await click(toggle())                          // its button closes it as well
    expect(panel()).toBeNull()
  })

  it('does not ask again while an answer is still on its way', async () => {
    let answer!: (r: MonitorReading) => void
    asked.mockReturnValue(new Promise<MonitorReading>((done) => { answer = done }))
    await render()
    await click(toggle())
    await later(9000)
    expect(asked).toHaveBeenCalledTimes(1)
    expect(panel()!.textContent).toContain('Reading the machine…')
    asked.mockResolvedValue(READING)
    await act(async () => { answer(READING) })
    expect(panel()!.textContent).not.toContain('Reading the machine…')
    await later(3000)
    expect(asked).toHaveBeenCalledTimes(2)
  })

  it('says so when the server does not answer, and keeps the figures it had', async () => {
    await render()
    await click(toggle())
    asked.mockRejectedValue(new Error('down'))
    await later(3000)
    expect(panel()!.querySelector('[role="alert"]')!.textContent).toBe('The server did not answer the last look: these figures are from before.')
    expect(figure('RAM')).toBe('24.9 / 30.2 GiB')
    asked.mockResolvedValue(READING)
    await later(3000)
    expect(panel()!.querySelector('[role="alert"]')).toBeNull()
  })

  it('says that the server does not answer when it never did', async () => {
    asked.mockRejectedValue(new Error('down'))
    await render()
    await click(toggle())
    expect(panel()!.querySelector('[role="alert"]')!.textContent).toBe('The server does not answer.')
    expect(panel()!.textContent).not.toContain('Reading the machine…')
  })

  it('says that no card answers on a machine without one', async () => {
    asked.mockResolvedValue({ ...READING, cards: [] })
    await render()
    await click(toggle())
    expect(panel()!.textContent).toContain('No NVIDIA card answers on this machine (nvidia-smi).')
    asked.mockResolvedValue(READING)
    await later(3000)
    expect(panel()!.textContent).not.toContain('No NVIDIA card answers')
  })

  it('does not ask while the page is not in view, and asks at once when it is again', async () => {
    let hidden = false
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
    try {
      await render()
      await click(toggle())
      hidden = true
      await later(9000)
      expect(asked).toHaveBeenCalledTimes(1)
      hidden = false
      await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })
      expect(asked).toHaveBeenCalledTimes(2)
    } finally {
      delete (document as { hidden?: boolean }).hidden
    }
  })

  it('shows a temperature near its limit as such, by the colour of its row and in its figure', async () => {
    asked.mockResolvedValue({ ...READING, cards: [{ ...CARD, temp: 80 }] })
    await render()
    await click(toggle())
    expect(rowOf('Temperature').className).toBe('meter-row warn')
    expect(figure('Temperature')).toBe('80°C of 85°C')
    expect(rowOf('RAM').className).toBe('meter-row')
  })

  it('says how often it asks, and what that means for a card', async () => {
    await render()
    await click(toggle())
    expect(panel()!.querySelector('.hint')!.textContent).toBe(
      'Read every three seconds while this is open, which keeps a graphics card awake that a laptop would put to sleep. Close it when you do not need it.')
  })
})
