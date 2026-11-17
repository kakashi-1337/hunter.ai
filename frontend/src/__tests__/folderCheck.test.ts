// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { FolderCheck } from '../components/FolderCheck'
import { MessageItem } from '../components/MessageItem'
import { RunChanges } from '../components/RunChanges'
import { useStore } from '../store'
import { checkStepLine, checkView, runningCheck } from '../tree'
import type { Block, ChangesBlock, CheckResult, Message, Step } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const result = (over: Partial<CheckResult> = {}): CheckResult => ({ passed: true, exit_code: 0, seconds: 3.2, timed_out: false, output: '', ...over })
const FAILED = result({ passed: false, exit_code: 1, output: 'FAILED tests/test_app.py::test_main\n1 failed in 0.4s\n' })
const check = (before: CheckResult | null, after: CheckResult | null, note = '') => ({ command: 'pytest -q', before, after, note })
const changes = (over: Partial<ChangesBlock> = {}): ChangesBlock => ({
  type: 'changes', run_id: 'r1', folder: '/home/dave/project', before: 'a'.repeat(40), after: 'b'.repeat(40), reason: '', undone: false, left: [],
  files: [{ path: 'app.py', change: 'changed' }], check: null, ...over,
})
const message = (blocks: Block[], status: Message['status'] = 'complete'): Message => ({
  id: 'm1', chat_id: 'c1', parent_id: null, role: 'assistant', blocks, provider: 'ollama', model: 'qwen3:8b',
  fallback_from: null, status, created_at: '2026-11-17T10:00:00Z', siblings: ['m1'],
})
const step = (over: Partial<Step>): Step => ({
  id: 's1', run_id: 'r1', seq: 1, kind: 'check', name: 'pytest -q', status: 'running', started_at: '2026-11-17T10:00:00Z', ended_at: null,
  input_tokens: 0, output_tokens: 0, detail: { when: 'after' }, ...over,
})

describe('what the check of the folder said, before and after a run', () => {
  it('says that the run made it pass', () => {
    expect(checkView(check(FAILED, result()))).toEqual({ text: 'failed before the run, passes after it.', level: 'ok' })
  })

  it('says that the run broke it, and how the check ended', () => {
    expect(checkView(check(result(), FAILED))).toEqual({ text: 'passed before the run, fails after it (exit code 1).', level: 'bad' })
  })

  it('says that it failed before the run and still does', () => {
    expect(checkView(check(FAILED, result({ passed: false, exit_code: 2 })))).toEqual({
      text: 'failed before the run and still fails after it (exit code 2).', level: 'warn' })
  })

  it('says that nothing changed for it', () => {
    expect(checkView(check(result(), result()))).toEqual({ text: 'passed before the run and after it.', level: '' })
  })

  it('says that a check which did not end was stopped, and counts it as failed', () => {
    const hung = result({ passed: false, exit_code: null, timed_out: true, seconds: 300 })
    expect(checkView(check(result(), hung))).toEqual({
      text: 'passed before the run, fails after it (it did not end within 300 seconds and was stopped).', level: 'bad' })
  })

  it('says that a check which could not be started did not run', () => {
    const none = result({ passed: false, exit_code: null, seconds: 0, output: 'not enough free disk for process output' })
    expect(checkView(check(result(), none)).text).toBe('passed before the run, fails after it (it could not be started).')
  })

  it('says so when there was no check before the run', () => {
    expect(checkView(check(null, FAILED))).toEqual({ text: 'fails after the run (exit code 1). It was not run before it.', level: 'warn' })
    expect(checkView(check(null, result()))).toEqual({ text: 'passes after the run. It was not run before it.', level: '' })
  })

  it('says why no check followed the run', () => {
    const note = 'No check followed this run: it was stopped, or it failed.'
    expect(checkView(check(FAILED, null, note))).toEqual({ text: `failed before the run (exit code 1). ${note}`, level: 'warn' })
    expect(checkView(check(result(), null, note))).toEqual({ text: `passed before the run. ${note}`, level: 'warn' })
  })
})

describe('the check that is going right now', () => {
  it('is the check step of the run that has not ended', () => {
    expect(runningCheck([step({ id: 's0', status: 'done', ended_at: '2026-11-17T10:00:03Z', detail: { when: 'before' } }), step({})]))
      .toEqual({ command: 'pytest -q', text: 'Running the check of this folder after the run: pytest -q' })
    expect(runningCheck([step({ detail: { when: 'before' } })])!.text).toBe('Running the check of this folder before the first change: pytest -q')
  })

  it('is none when no check is going', () => {
    expect(runningCheck([step({ status: 'done', ended_at: '2026-11-17T10:00:03Z' })])).toBeNull()
    expect(runningCheck([step({ kind: 'tool_call', name: 'execute' })])).toBeNull()
    expect(runningCheck([])).toBeNull()
  })

  it('is one line in the list of steps once it ended', () => {
    const over = (status: string, detail: Record<string, unknown>) => step({ status, ended_at: '2026-11-17T10:00:03Z', detail })
    expect(checkStepLine(over('done', { when: 'before', passed: true, exit_code: 0, seconds: 3.2, timed_out: false })))
      .toBe('before the first change · passed · 3.2 s')
    expect(checkStepLine(over('done', { when: 'after', passed: false, exit_code: 1, seconds: 0.4, timed_out: false })))
      .toBe('after the run · failed (exit code 1) · 0.4 s')
    expect(checkStepLine(over('done', { when: 'after', passed: false, exit_code: null, seconds: 300, timed_out: true })))
      .toBe('after the run · stopped: it did not end within 300 seconds')
    expect(checkStepLine(over('cancelled', { when: 'after', passed: false, exit_code: null, seconds: 0, timed_out: false })))
      .toBe('after the run · stopped with the run')
    expect(checkStepLine(over('error', { when: 'after', passed: false, exit_code: null, seconds: 0, timed_out: false })))
      .toBe('after the run · could not be started · 0 s')
    expect(checkStepLine(step({}))).toBe('after the run · running')
  })
})

describe('on the page', () => {
  let root: Root
  let host: HTMLElement
  beforeEach(() => {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
    useStore.setState({ messages: [], live: null, error: null, asks: [], runningCalls: [] })
  })
  afterEach(() => {
    act(() => root.unmount())
    host.remove()
    vi.restoreAllMocks()
  })
  const draw = async (block: ChangesBlock) => { await act(async () => { root.render(createElement(RunChanges, { block })) }) }

  it('the outcome of the check stands with the list, with what the check wrote', async () => {
    await draw(changes({ check: check(result(), FAILED) }))
    const line = host.querySelector('.run-check')!
    expect(line.querySelector('.verdict')!.textContent).toBe('Check pytest -q: passed before the run, fails after it (exit code 1).')
    expect(line.querySelector('.verdict code')!.textContent).toBe('pytest -q')
    expect(line.className).toContain('bad')
    const after = [...host.querySelectorAll('details')].find((d) => d.querySelector('summary')!.textContent === 'What the check wrote after the run (3.2 s)')!
    expect(after.open).toBe(true)
    expect(after.querySelector('pre')!.textContent).toBe('FAILED tests/test_app.py::test_main\n1 failed in 0.4s\n')
  })

  it('what a passing check wrote is there to open, not in the way', async () => {
    await draw(changes({ check: check(FAILED, result({ output: '1 passed in 0.3s\n' })) }))
    const all = [...host.querySelectorAll('details')].filter((d) => d.querySelector('summary')!.textContent!.startsWith('What the check wrote'))
    expect(all.map((d) => [d.querySelector('summary')!.textContent, d.open])).toEqual([
      ['What the check wrote after the run (3.2 s)', false], ['What the check wrote before the run (3.2 s)', false]])
  })

  it('a check that wrote nothing has nothing to open', async () => {
    await draw(changes({ check: check(result(), result()) }))
    expect(host.querySelector('.run-check')!.textContent).toBe('Check pytest -q: passed before the run and after it.')
    expect([...host.querySelectorAll('summary')].map((s) => s.textContent)).toEqual(['The file'])
  })

  it('a list without a check shows none', async () => {
    await draw(changes())
    expect(host.querySelector('.run-check')).toBeNull()
  })

  it('what the check wrote is shown with hidden characters made visible', async () => {
    await draw(changes({ check: check(result(), result({ passed: false, exit_code: 1, output: 'ok‮deliaf\n' })) }))
    expect(host.querySelector('.run-check pre')!.textContent).toBe('ok\\u{202E}deliaf\n')
  })

  it('the answer says that the check is running while it is', async () => {
    useStore.setState({ live: { runId: 'r1', chatId: 'c1', target: 'm1', steps: [step({})] } })
    await act(async () => { root.render(createElement(MessageItem, { message: message([{ type: 'text', text: 'Fixed.' }], 'streaming') })) })
    expect(host.querySelector('[role="status"]')!.textContent).toBe('Running the check of this folder after the run: pytest -q')
    useStore.setState({ live: { runId: 'r1', chatId: 'c1', target: 'm1', steps: [step({ status: 'done', ended_at: '2026-11-17T10:00:03Z' })] } })
    await act(async () => { root.render(createElement(MessageItem, { message: message([{ type: 'text', text: 'Fixed.' }], 'streaming') })) })
    expect(host.querySelector('[role="status"]')).toBeNull()
  })

  it('an answer of an earlier run says nothing about a check that is running now', async () => {
    useStore.setState({ live: { runId: 'r2', chatId: 'c1', target: 'm2', steps: [step({ run_id: 'r2' })] } })
    await act(async () => { root.render(createElement(MessageItem, { message: message([{ type: 'text', text: 'Fixed.' }]) })) })
    expect(host.querySelector('[role="status"]')).toBeNull()
  })
})

describe('the check of a folder, where the owner sets it', () => {
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
  const draw = async (folder = '/home/dave/project') => { await act(async () => { root.render(createElement(FolderCheck, { chatId: 'c1', folder })) }) }
  const input = () => host.querySelector('input[aria-label="Check command"]') as HTMLInputElement
  const type = async (text: string) => {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input(), text)
      input().dispatchEvent(new Event('input', { bubbles: true }))
    })
  }
  const press = async (name: string) => {
    const button = [...host.querySelectorAll('button')].find((b) => b.textContent === name)!
    await act(async () => { button.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  }

  it('shows the command that is set, and says what setting one means', async () => {
    vi.spyOn(api, 'getCheck').mockResolvedValue({ folder: '/home/dave/project', command: 'pytest -q' })
    await draw()
    expect(input().value).toBe('pytest -q')
    expect(host.textContent).toContain('HunterAI runs it in this folder by itself, in coding mode: before a run changes anything, and after the run.')
    expect(host.textContent).toContain('It runs whatever the command runs, files the model has just written included, and does not ask.')
  })

  it('saves what the owner typed, for the folder', async () => {
    vi.spyOn(api, 'getCheck').mockResolvedValue({ folder: '/home/dave/project', command: '' })
    const put = vi.spyOn(api, 'putCheck').mockResolvedValue({ folder: '/home/dave/project', command: 'npm test' })
    await draw()
    await type('  npm test ')
    await press('Save')
    expect(put).toHaveBeenCalledWith('c1', '  npm test ')
    expect(input().value).toBe('npm test')
    expect(host.querySelector('[role="status"]')!.textContent).toBe('Saved for /home/dave/project. Every chat that works in this folder uses it.')
  })

  it('removes the check', async () => {
    vi.spyOn(api, 'getCheck').mockResolvedValue({ folder: '/home/dave/project', command: 'pytest -q' })
    const put = vi.spyOn(api, 'putCheck').mockResolvedValue({ folder: '/home/dave/project', command: '' })
    await draw()
    await press('Remove')
    expect(put).toHaveBeenCalledWith('c1', '')
    expect(input().value).toBe('')
    expect(host.querySelector('[role="status"]')!.textContent).toBe('Removed. No check runs in /home/dave/project.')
  })

  it('shows why the server did not take a command, and keeps what was typed', async () => {
    vi.spyOn(api, 'getCheck').mockResolvedValue({ folder: '/home/dave/project', command: '' })
    vi.spyOn(api, 'putCheck').mockRejectedValue(new api.ApiError(400, "The check was not saved: the command addresses HunterAI's own API, which nothing HunterAI runs by itself may call."))
    await draw()
    await type('curl http://127.0.0.1:8790/api/settings')
    await press('Save')
    expect(host.querySelector('[role="alert"]')!.textContent).toContain("The check was not saved: the command addresses HunterAI's own API")
    expect(input().value).toBe('curl http://127.0.0.1:8790/api/settings')
  })

  it('reads the check again when the chat works in another folder', async () => {
    const get = vi.spyOn(api, 'getCheck').mockResolvedValueOnce({ folder: '/home/dave/project', command: 'pytest -q' })
      .mockResolvedValueOnce({ folder: '/home/dave/other', command: '' })
    await draw()
    await draw('/home/dave/other')
    expect(get).toHaveBeenCalledTimes(2)
    expect(input().value).toBe('')
  })
})
