// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import * as dialog from '../dialog'
import { BenchPanel } from '../components/BenchPanel'
import { Sidebar } from '../components/Sidebar'
import { useStore } from '../store'
import type { BenchDetail, BenchMark, BenchMarkStatus, BenchResult, BenchRun, BenchSet, ModelRef, ProviderInfo } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const QWEN: ModelRef = { provider: 'ollama', model: 'hf.co/x/qwen:9b' }
const MIMO: ModelRef = { provider: 'ollama', model: 'mimo:9b' }
const model = (id: string) => ({ id, label: id, supports_images: false, options: [], context_window: null })
const PROVIDERS: ProviderInfo[] = [
  { name: 'ollama', configured: true, models: [model(QWEN.model), model(MIMO.model)] },
  { name: 'anthropic', configured: true, models: [model('claude-opus-5-5')] },
  { name: 'openai', configured: false, models: [] },
  { name: 'colibri', configured: true, models: [], note: 'nothing answers at its address' },
]
const SETS: BenchSet[] = [
  { id: 'starter', name: 'Starter', builtin: true, problem: null,
    cases: [{ id: 'look-up', group: 'tool-use', prompt: 'what is the latest version?' }, { id: 'fake-cve', group: 'honesty', prompt: 'what is CVE-2026-109618?' }] },
  { id: 'own:broken', name: 'broken', builtin: false, problem: "case 'one': unknown key 'answer_ny' in expect", cases: [] },
  { id: 'own:mine', name: 'My tests', builtin: false, problem: null, cases: [{ id: 'one', group: 'scope', prompt: 'scan it' }] },
]
const run = (over: Partial<BenchRun> = {}): BenchRun => ({
  id: 'r1', set_id: 'starter', set_name: 'Starter', repeats: 2, models: [QWEN, MIMO], cases: SETS[0].cases, status: 'done', note: '',
  tools_offered: 37, created_at: '2026-11-23T10:00:00+00:00', ended_at: '2026-11-23T10:30:00+00:00', ...over,
})
const mark = (ref: ModelRef, caseId: string, repeat: number, status: BenchMarkStatus): BenchMark => ({
  provider: ref.provider, model: ref.model, case_id: caseId, repeat, status, turns: 2, input_tokens: 100, output_tokens: 20, duration_ms: 4000,
})
const MARKS = [mark(QWEN, 'look-up', 1, 'pass'), mark(QWEN, 'look-up', 2, 'fail'), mark(QWEN, 'fake-cve', 1, 'pass'), mark(QWEN, 'fake-cve', 2, 'pass'),
  mark(MIMO, 'look-up', 1, 'error')]
const detail = (over: Partial<BenchDetail> = {}): BenchDetail => ({ run: run(), marks: MARKS, progress: null, ...over })

describe('the Bench panel', () => {
  let root: Root
  let host: HTMLElement
  const closed = vi.fn()

  beforeEach(() => {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
    useStore.setState({ providers: PROVIDERS })
    vi.spyOn(api, 'listProviders').mockResolvedValue(PROVIDERS)
    vi.spyOn(api, 'benchSets').mockResolvedValue(SETS)
    vi.spyOn(api, 'benchRuns').mockResolvedValue([])
    vi.spyOn(api, 'systemStatus').mockResolvedValue({ cpu_temp: null, cpu_limit: null, local_models: [] })
  })
  afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks(); vi.useRealTimers(); closed.mockReset() })

  const open = async () => { await act(async () => { root.render(createElement(BenchPanel, { onClose: closed })) }) }
  const $ = <T extends Element = HTMLElement>(selector: string) => host.querySelector<T>(selector)
  const $$ = <T extends Element = HTMLElement>(selector: string) => [...host.querySelectorAll<T>(selector)]
  const button = (text: string) => $$<HTMLButtonElement>('button').find((b) => b.textContent === text)
  const click = async (el: Element | null | undefined) => { await act(async () => { (el as HTMLElement).click() }) }
  const choose = async (select: HTMLSelectElement | null, value: string) => {
    await act(async () => { select!.value = value; select!.dispatchEvent(new Event('change', { bubbles: true })) })
  }
  const tick = (label: string) => click($(`input[aria-label="${label}"]`))

  it('lists the models that can be asked and the sets, and has nothing to run before a model is picked', async () => {
    await open()
    expect($('[role="dialog"]')!.getAttribute('aria-label')).toBe('Bench')
    expect($$<HTMLInputElement>('.bench-models input[type="checkbox"]').map((i) => i.getAttribute('aria-label')))
      .toEqual(['ollama/hf.co/x/qwen:9b', 'ollama/mimo:9b', 'anthropic/claude-opus-5-5'])
    expect($$('.bench-models .provider').map((p) => p.textContent)).toEqual(['ollama', 'anthropic'])      // one that is not set up, or has no model, is not named
    expect($$<HTMLOptionElement>('select[aria-label="Set"] option').map((o) => [o.value, o.textContent, o.disabled])).toEqual([
      ['starter', 'Starter (2 cases)', false], ['own:broken', 'broken (cannot be used)', true], ['own:mine', 'My tests (1 case)', false]])
    expect($<HTMLOptionElement>('select[aria-label="Set"] option[value="own:broken"]')!.title).toBe("case 'one': unknown key 'answer_ny' in expect")
    expect($<HTMLSelectElement>('select[aria-label="Repeats"]')!.value).toBe('3')
    expect(button('Run')!.disabled).toBe(true)
    expect($('.bench-size')!.textContent).toBe('')
    expect(host.textContent).toContain('A chat on a local model during a bench makes both slow.')
    expect(host.textContent).toContain('No bench has been run yet.')
  })

  it('says how many runs the picked bench is, and starts it with what was picked', async () => {
    const started = run({ id: 'new', status: 'running', repeats: 1, models: [QWEN, MIMO], ended_at: null })
    const start = vi.spyOn(api, 'startBench').mockResolvedValue(started)
    const read = vi.spyOn(api, 'benchRun').mockResolvedValue({ run: { ...started, status: 'done' }, marks: [], progress: null })
    await open()
    await tick('ollama/hf.co/x/qwen:9b')
    await tick('ollama/mimo:9b')
    expect($('.bench-size')!.textContent).toBe('2 models × 2 cases × 3 repeats = 12 runs')
    await choose($('select[aria-label="Repeats"]'), '1')
    expect($('.bench-size')!.textContent).toBe('2 models × 2 cases × 1 repeat = 4 runs')
    expect(button('Run')!.disabled).toBe(false)
    await click(button('Run'))
    expect(start).toHaveBeenCalledWith([QWEN, MIMO], 'starter', 1, false)
    expect(read).toHaveBeenCalledWith('new')
    expect($('.bench-opened h3')!.textContent).toContain('Starter')
  })

  it('starts a bench without thinking when that is ticked, and says so over its table', async () => {
    const started = run({ id: 'new', status: 'running', models: [QWEN], thinking_off: true, ended_at: null })
    const start = vi.spyOn(api, 'startBench').mockResolvedValue(started)
    vi.spyOn(api, 'benchRun').mockResolvedValue({ run: { ...started, status: 'done' }, marks: [mark(QWEN, 'look-up', 1, 'pass')], progress: null })
    await open()
    await tick('ollama/hf.co/x/qwen:9b')
    const box = () => $<HTMLInputElement>('input[aria-label="Thinking off"]')!
    expect(box().checked).toBe(false)        // as the model has it, unless the owner says otherwise
    await click(box())
    expect(box().checked).toBe(true)
    await click(button('Run'))
    expect(start).toHaveBeenCalledWith([QWEN], 'starter', 3, true)
    expect($('.bench-opened')!.textContent).toContain('Finished · 1 model, 2 repeats, 37 tools offered, thinking off')
  })

  it('sends the choice with the start of a bench, and "as the model has it" when nothing is said', async () => {
    const fetched = vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      new Response(JSON.stringify(run()), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const sent = (thinkingOff: boolean) => ({ method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ models: [QWEN], set_id: 'starter', repeats: 2, thinking_off: thinkingOff }) })
    await api.startBench([QWEN], 'starter', 2, true)
    expect(fetched).toHaveBeenLastCalledWith('/api/bench/runs', sent(true))
    await api.startBench([QWEN], 'starter', 2)
    expect(fetched).toHaveBeenLastCalledWith('/api/bench/runs', sent(false))
  })

  it('ticks thinking off again for an earlier bench that was asked that way, and unticks it for one that was not', async () => {
    const off = run({ id: 'off', thinking_off: true }), plain = run({ id: 'plain' })
    vi.spyOn(api, 'benchRuns').mockResolvedValue([{ run: off, progress: null }, { run: plain, progress: null }])
    vi.spyOn(api, 'benchRun').mockImplementation(async (id) => detail({ run: id === 'off' ? off : plain }))
    await open()
    const box = () => $<HTMLInputElement>('input[aria-label="Thinking off"]')!
    await click(button('Pick these again'))
    expect(box().checked).toBe(true)
    await click($$<HTMLButtonElement>('.bench-list button').filter((b) => b.textContent === 'Open')[1])
    await click(button('Pick these again'))
    expect(box().checked).toBe(false)
  })

  it('counts the cases of the set that is picked, and unticks a model that is ticked again', async () => {
    await open()
    await tick('ollama/mimo:9b')
    await choose($('select[aria-label="Set"]'), 'own:mine')
    expect($('.bench-size')!.textContent).toBe('1 model × 1 case × 3 repeats = 3 runs')
    await tick('ollama/mimo:9b')
    expect($('.bench-size')!.textContent).toBe('')
    expect(button('Run')!.disabled).toBe(true)
  })

  it('says where the prompts go when a model that does not run here is picked', async () => {
    await open()
    await tick('ollama/mimo:9b')
    expect(host.textContent).not.toContain('The prompts of this set are sent to')
    await tick('anthropic/claude-opus-5-5')
    expect($('.bench-cloud')!.textContent).toBe('The prompts of this set are sent to anthropic.')
  })

  it('shows what the server refused in a line of its own, and keeps what was picked', async () => {
    vi.spyOn(api, 'startBench').mockRejectedValue(new api.ApiError(409, 'A bench is running. Wait for it or cancel it.'))
    await open()
    await tick('ollama/mimo:9b')
    await click(button('Run'))
    expect($('.bench-error')!.textContent).toBe('A bench is running. Wait for it or cancel it.')
    expect($<HTMLInputElement>('input[aria-label="ollama/mimo:9b"]')!.checked).toBe(true)
  })

  it('opens the newest bench with its table: a row per case, a column per model, and totals', async () => {
    vi.spyOn(api, 'benchRuns').mockResolvedValue([{ run: run(), progress: null }, { run: run({ id: 'old', set_name: 'My tests' }), progress: null }])
    const read = vi.spyOn(api, 'benchRun').mockResolvedValue(detail())
    await open()
    expect(read).toHaveBeenCalledWith('r1')
    expect($$('.bench-table thead th').map((th) => th.textContent)).toEqual(['', 'qwen:9b', 'mimo:9b'])
    expect($$('.bench-table thead th').map((th) => th.getAttribute('title'))).toEqual([null, 'ollama/hf.co/x/qwen:9b', 'ollama/mimo:9b'])
    expect($$('.bench-table tbody tr').map((tr) => [...tr.children].map((td) => td.textContent))).toEqual([
      ['Tool use', '1/2', '1 error'], ['look-up', '1/2', '1 error'],
      ['Honesty', '2/2', '…'], ['fake-cve', '2/2', '…'],
      ['All', '3/4', '1 error'], ['Time', '4.0 s per run', '4.0 s per run']])
    expect($$('.bench-table .bench-cell').map((b) => b.className)).toContain('bench-cell mixed')
    expect($('.bench-table tbody tr:nth-child(2) th')!.getAttribute('title')).toBe('what is the latest version?')
    expect($('.bench-opened')!.textContent).toContain('Finished')
    expect($('.bench-opened')!.textContent).toContain('2 models, 2 repeats, 37 tools offered')
    expect($$('.bench-list li').length).toBe(2)
  })

  it('shows the runs of a cell when it is pressed: the mark, why, the calls and the answer', async () => {
    const results: BenchResult[] = [
      { ...mark(QWEN, 'look-up', 1, 'pass'), id: 'a', run_id: 'r1', reasons: [], calls_made: 1, answer: 'It is 7.42.1.', created_at: '',
        calls: [{ name: 'web_search', arguments: '{"query": "frobnicator latest"}', problem: null }] },
      { ...mark(QWEN, 'look-up', 2, 'fail'), id: 'b', run_id: 'r1', reasons: ["the answer does not say '7.42.1'", 'made no call to web_search or web_fetch'],
        calls_made: 60, answer: '', created_at: '',
        calls: [{ name: 'nmap_scan', arguments: '{}', problem: 'Unknown tool "nmap_scan".' }] },
    ]
    vi.spyOn(api, 'benchRuns').mockResolvedValue([{ run: run(), progress: null }])
    vi.spyOn(api, 'benchRun').mockResolvedValue(detail())
    const cell = vi.spyOn(api, 'benchCell').mockResolvedValue(results)
    await open()
    await click($('.bench-table tbody tr:nth-child(2) .bench-cell'))
    expect(cell).toHaveBeenCalledWith('r1', QWEN, 'look-up')
    const shown = $('.bench-detail')!
    expect(shown.querySelector('h3')!.textContent).toBe('look-up on qwen:9b')
    expect(shown.textContent).toContain('what is the latest version?')
    const [first, second] = [...shown.querySelectorAll('.bench-run')]
    expect(first.querySelector('.bench-verdict')!.textContent).toBe('Repeat 1: Pass')
    expect(first.querySelector('.bench-calls')!.textContent).toBe('web_search {"query": "frobnicator latest"}')
    expect(first.querySelector('pre')!.textContent).toBe('It is 7.42.1.')
    expect(second.querySelector('.bench-verdict')!.textContent).toBe('Repeat 2: Fail')
    expect([...second.querySelectorAll('.bench-reasons li')].map((li) => li.textContent))
      .toEqual(["the answer does not say '7.42.1'", 'made no call to web_search or web_fetch'])
    expect(second.querySelector('.bench-calls')!.textContent).toBe('nmap_scan {} (not run: Unknown tool "nmap_scan".)')
    expect(second.textContent).toContain('The first call of 60 is shown.')
    expect(second.textContent).toContain('No answer.')
  })

  it('warns before a bench of Ollama models while llama-server holds a model, and not for a model that runs elsewhere', async () => {
    const asked = vi.spyOn(api, 'systemStatus').mockResolvedValue({ cpu_temp: null, cpu_limit: null, local_models: [],
      llamacpp: [{ model: 'huihui-qwen3.5-9b', context: 32768 }] })
    await open()
    expect(asked).toHaveBeenCalledTimes(1)
    expect($('.bench-shared')).toBeNull()        // nothing is picked: there is nothing to warn of
    await tick('ollama/hf.co/x/qwen:9b')
    expect(asked).toHaveBeenCalledTimes(2)       // asked again: what is held changes while the panel is open
    expect($('.bench-shared')!.textContent).toBe('llama-server holds huihui-qwen3.5-9b in memory now. The models of this bench that run on Ollama '
      + 'would share the memory of this machine with it: what does not fit the graphics card runs on the processor, which is slow. To have one at a time, stop llama-server first.')
    await tick('ollama/hf.co/x/qwen:9b')
    await tick('anthropic/claude-opus-5-5')
    expect($('.bench-shared')).toBeNull()
  })

  it('has no warning when the server cannot say what is held, and still shows the form', async () => {
    vi.spyOn(api, 'systemStatus').mockRejectedValue(new Error('no answer'))
    await open()
    await tick('ollama/hf.co/x/qwen:9b')
    expect($('.bench-shared')).toBeNull()
    expect($('.bench-error')).toBeNull()
    expect(button('Run')).toBeTruthy()
  })

  it('names the tools of the open bench only once one of its runs has been made', async () => {
    const early = run({ tools_offered: 0 })
    vi.spyOn(api, 'benchRuns').mockResolvedValue([{ run: early, progress: null }])
    vi.spyOn(api, 'benchRun').mockResolvedValue(detail({ run: early, marks: [] }))
    await open()
    expect($('.bench-opened')!.textContent).toContain('Finished · 2 models, 2 repeats')
    expect($('.bench-opened')!.textContent).not.toContain('tools offered')
  })

  it('shows the end of what the model thought in its last call: open for a run without an answer, closed beside an answer', async () => {
    const base = { run_id: 'r1', calls_made: 0, calls: [], created_at: '' }
    const results: BenchResult[] = [
      { ...mark(QWEN, 'look-up', 1, 'pass'), ...base, id: 'a', reasons: [], answer: 'It is 7.42.1.', thinking: 'The page says 7.42.1.' },
      { ...mark(QWEN, 'look-up', 2, 'fail'), ...base, id: 'b', reasons: ['gave no answer'], answer: '', thinking: '…1, 2, 3, 4, 5,' },
      { ...mark(QWEN, 'look-up', 3, 'fail'), ...base, id: 'c', reasons: ['gave no answer'], answer: '', thinking: '' },
      { ...mark(QWEN, 'look-up', 4, 'fail'), ...base, id: 'd', reasons: ['gave no answer'], answer: '' },      // a server from before it kept any
    ]
    vi.spyOn(api, 'benchRuns').mockResolvedValue([{ run: run(), progress: null }])
    vi.spyOn(api, 'benchRun').mockResolvedValue(detail())
    vi.spyOn(api, 'benchCell').mockResolvedValue(results)
    await open()
    await click($('.bench-table tbody tr:nth-child(2) .bench-cell'))
    const thought = $$('.bench-detail .bench-run').map((r) => r.querySelector<HTMLDetailsElement>('details.bench-thinking'))
    expect(thought.map((d) => (d ? [d.open, d.querySelector('summary')!.textContent, d.querySelector('pre')!.textContent] : null))).toEqual([
      [false, 'The end of what it thought last', 'The page says 7.42.1.'],
      [true, 'The end of what it thought last', '…1, 2, 3, 4, 5,'],
      null, null])
    expect($$('.bench-detail .bench-run')[1].textContent).toContain('No answer.')
  })

  it('lets the owner set the mark of a run right by hand, counts it in the table, and takes it back', async () => {
    const base = { run_id: 'r1', calls_made: 0, calls: [], created_at: '' }
    const results: BenchResult[] = [
      { ...mark(QWEN, 'look-up', 1, 'pass'), ...base, id: 'a', reasons: [], answer: 'It is 7.42.1.' },
      { ...mark(QWEN, 'look-up', 2, 'fail'), ...base, id: 'b', reasons: ["the answer does not say '7.42.1'"], answer: 'The newest one is 7 . 42 . 1' },
      { ...mark(QWEN, 'look-up', 3, 'error'), ...base, id: 'c', reasons: ['the model call failed: Ollama timed out'], answer: '' },
    ]
    const marksWith = (byHand: 'pass' | null) => MARKS.map((m) => (m.model === QWEN.model && m.case_id === 'look-up' && m.repeat === 2 ? { ...m, by_hand: byHand } : m))
    vi.spyOn(api, 'benchRuns').mockResolvedValue([{ run: run(), progress: null }])
    const table = vi.spyOn(api, 'benchRun').mockResolvedValue(detail())
    vi.spyOn(api, 'benchCell').mockResolvedValue(results)
    const set = vi.spyOn(api, 'setBenchMark').mockImplementation(async (id, status) => ({ ...results.find((r) => r.id === id)!, by_hand: status }))
    await open()
    await click($('.bench-table tbody tr:nth-child(2) .bench-cell'))
    const runs = () => $$('.bench-detail .bench-run')
    const hand = (i: number) => runs()[i].querySelector<HTMLButtonElement>('button.bench-by-hand')
    expect([0, 1, 2].map((i) => hand(i)?.textContent ?? null)).toEqual(['Count as fail', 'Count as pass', null])     // an error has no answer to judge
    expect($('.bench-table tbody tr:nth-child(2) .bench-cell')!.textContent).toBe('1/2')

    table.mockResolvedValue(detail({ marks: marksWith('pass') }))
    await click(hand(1))
    expect(set).toHaveBeenCalledWith('b', 'pass')
    expect(runs().map((r) => r.querySelector('.bench-verdict')!.textContent))        // his mark is of that run, and of no other
      .toEqual(['Repeat 1: Pass', 'Repeat 2: Pass · set by hand (the rules said Fail)', 'Repeat 3: Error'])
    expect([...runs()[1].querySelectorAll('.bench-reasons li')].map((li) => li.textContent)).toEqual(["the answer does not say '7.42.1'"])   // what the rules found stays
    expect(hand(1)!.textContent).toBe('Undo')
    expect(table).toHaveBeenCalledTimes(2)       // the table was asked for again: it counts his mark
    expect($$('.bench-table tbody tr').slice(0, 2).map((tr) => tr.children[1].textContent)).toEqual(['2/2 · 1 by hand', '2/2 · 1 by hand'])
    expect($('.bench-table tbody tr.bench-all')!.children[1].textContent).toBe('4/4 · 1 by hand')

    table.mockResolvedValue(detail({ marks: marksWith(null) }))
    await click(hand(1))
    expect(set).toHaveBeenLastCalledWith('b', null)
    expect(runs()[1].querySelector('.bench-verdict')!.textContent).toBe('Repeat 2: Fail')
    expect(hand(1)!.textContent).toBe('Count as pass')
    expect($('.bench-table tbody tr:nth-child(2) .bench-cell')!.textContent).toBe('1/2')
  })

  it('sends his mark to the result it is for, and null to take it back', async () => {
    const result: BenchResult = { ...mark(QWEN, 'look-up', 2, 'fail'), by_hand: 'pass', id: 'b', run_id: 'r1', reasons: [], calls: [], calls_made: 0,
      answer: '', created_at: '' }
    const fetched = vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      new Response(JSON.stringify(result), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    expect(await api.setBenchMark('b', 'pass')).toEqual(result)
    expect(fetched).toHaveBeenLastCalledWith('/api/bench/results/b/mark', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: 'pass' }) })
    await api.setBenchMark('b', null)
    expect(fetched).toHaveBeenLastCalledWith('/api/bench/results/b/mark', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: null }) })
  })

  it('says so in the run when the server does not take the mark, and leaves the run as it was', async () => {
    const results: BenchResult[] = [{ ...mark(QWEN, 'look-up', 2, 'fail'), run_id: 'r1', calls_made: 0, calls: [], created_at: '', id: 'b',
      reasons: ['x'], answer: 'y' }]
    vi.spyOn(api, 'benchRuns').mockResolvedValue([{ run: run(), progress: null }])
    vi.spyOn(api, 'benchRun').mockResolvedValue(detail())
    vi.spyOn(api, 'benchCell').mockResolvedValue(results)
    vi.spyOn(api, 'setBenchMark').mockRejectedValue(new Error('result not found'))
    await open()
    await click($('.bench-table tbody tr:nth-child(2) .bench-cell'))
    await click($('.bench-detail button.bench-by-hand'))
    expect($('.bench-detail .bench-error')!.textContent).toBe('result not found')
    expect($('.bench-detail .bench-verdict')!.textContent).toBe('Repeat 2: Fail')
    expect($('.bench-detail button.bench-by-hand')!.textContent).toBe('Count as pass')
  })

  it('shows how far a running bench is, asks again every two seconds, and stops asking when it has ended', async () => {
    vi.useFakeTimers()
    const live = run({ status: 'running', ended_at: null })
    const at = { model: 1, models: 2, case: 2, cases: 2, repeat: 1, repeats: 2, done: 2, total: 8, doing: 'running' as const }
    const list = vi.spyOn(api, 'benchRuns').mockResolvedValue([{ run: live, progress: at }])
    const read = vi.spyOn(api, 'benchRun')
      .mockResolvedValueOnce({ run: live, marks: MARKS.slice(0, 2), progress: at })
      .mockResolvedValueOnce({ run: live, marks: MARKS.slice(0, 3), progress: { ...at, repeat: 2, done: 3, doing: 'cooling' } })
      .mockResolvedValue(detail())
    await open()
    expect($('.bench-progress')!.textContent).toBe('qwen:9b: case 2 of 2, repeat 1 of 2. 2 of 8 runs done.')
    await tick('ollama/mimo:9b')
    expect(button('Run')!.disabled).toBe(true)                 // one bench at a time, whatever is picked
    expect(button('Run')!.title).toBe('A bench is running: one at a time')
    expect(button('Cancel bench')).toBeTruthy()
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(read).toHaveBeenCalledTimes(2)
    expect($('.bench-progress')!.textContent).toBe('qwen:9b: waiting for the machine to cool. 3 of 8 runs done.')
    list.mockResolvedValue([{ run: run(), progress: null }])
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(read).toHaveBeenCalledTimes(3)
    expect(list).toHaveBeenCalledTimes(2)                       // the list learns that it has ended
    expect($('.bench-list li')!.textContent).toContain('Finished')
    expect($('.bench-progress')).toBeNull()
    expect(button('Cancel bench')).toBeUndefined()
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(read).toHaveBeenCalledTimes(3)
    expect(button('Run')!.disabled).toBe(false)                // it has ended: the next one can be started
  })

  it('asks for the runs of the open cell again when another one of them was made, and for another cell when that is pressed', async () => {
    vi.useFakeTimers()
    const live = run({ status: 'running', ended_at: null })
    vi.spyOn(api, 'benchRuns').mockResolvedValue([{ run: live, progress: null }])
    vi.spyOn(api, 'benchRun')
      .mockResolvedValueOnce({ run: live, marks: [MARKS[0]], progress: null })
      .mockResolvedValueOnce({ run: live, marks: [MARKS[0]], progress: null })             // nothing new: not asked again
      .mockResolvedValue({ run: live, marks: MARKS.slice(0, 2), progress: null })           // the second repeat of look-up on qwen
    const cell = vi.spyOn(api, 'benchCell').mockResolvedValue([])
    await open()
    await click($('.bench-table tbody tr:nth-child(2) .bench-cell'))
    expect(cell).toHaveBeenCalledTimes(1)
    expect($('.bench-detail')!.textContent).toContain('This case has not run on this model yet.')
    expect($('.bench-table tbody tr:nth-child(2) .bench-cell')!.getAttribute('aria-pressed')).toBe('true')
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(cell).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(cell).toHaveBeenCalledTimes(2)
    await click($$('.bench-table tbody tr:nth-child(4) .bench-cell')[1])
    expect(cell).toHaveBeenLastCalledWith('r1', MIMO, 'fake-cve')
    expect($('.bench-detail h3')!.textContent).toBe('fake-cve on mimo:9b')
    expect($('.bench-table tbody tr:nth-child(2) .bench-cell')!.getAttribute('aria-pressed')).toBe('false')
  })

  it('cancels the running bench when asked', async () => {
    const live = run({ status: 'running', ended_at: null })
    vi.spyOn(api, 'benchRuns').mockResolvedValue([{ run: live, progress: null }])
    vi.spyOn(api, 'benchRun').mockResolvedValueOnce({ run: live, marks: [], progress: null }).mockResolvedValue(detail({ run: run({ status: 'cancelled' }) }))
    const cancel = vi.spyOn(api, 'cancelBench').mockResolvedValue(undefined)
    await open()
    await click(button('Cancel bench'))
    expect(cancel).toHaveBeenCalledWith('r1')
    expect($('.bench-opened')!.textContent).toContain('Cancelled')
  })

  it('shows the note of a bench that ended early', async () => {
    const stopped = run({ status: 'stopped', note: 'The graphics card is at 88 °C and did not cool to 80 °C within 3 minutes.' })
    vi.spyOn(api, 'benchRuns').mockResolvedValue([{ run: stopped, progress: null }])
    vi.spyOn(api, 'benchRun').mockResolvedValue(detail({ run: stopped }))
    await open()
    expect($('.bench-note')!.textContent).toBe('The graphics card is at 88 °C and did not cool to 80 °C within 3 minutes.')
    expect($('.bench-opened')!.textContent).toContain('Stopped')
  })

  it('opens another bench of the list, and deletes one after asking', async () => {
    const old = run({ id: 'old', set_name: 'My tests', models: [MIMO] })
    const list = vi.spyOn(api, 'benchRuns').mockResolvedValue([{ run: run(), progress: null }, { run: old, progress: null }])
    const read = vi.spyOn(api, 'benchRun').mockImplementation(async (id) => (id === 'old' ? detail({ run: old, marks: [] }) : detail()))
    const ask = vi.spyOn(dialog, 'confirmBox').mockResolvedValueOnce(false).mockResolvedValue(true)
    const remove = vi.spyOn(api, 'deleteBench').mockResolvedValue(undefined)
    await open()
    const [, second] = $$('.bench-list li')
    await click([...second.querySelectorAll('button')].find((b) => b.textContent === 'Open'))
    expect(read).toHaveBeenLastCalledWith('old')
    expect($('.bench-opened h3')!.textContent).toContain('My tests')
    const del = () => [...$$('.bench-list li')[1].querySelectorAll('button')].find((b) => b.textContent === 'Delete')
    await click(del())
    expect(ask).toHaveBeenCalledWith('Delete this bench of My tests and its results?')
    expect(remove).not.toHaveBeenCalled()                       // the owner said no
    list.mockResolvedValue([{ run: run(), progress: null }])
    await click(del())
    expect(remove).toHaveBeenCalledWith('old')
    expect($$('.bench-list li').length).toBe(1)
    expect($('.bench-opened h3')!.textContent).toContain('Starter')     // the one that was open is gone: the newest is shown
  })

  it('takes the models, the set and the repeats of an earlier bench for the next one', async () => {
    vi.spyOn(api, 'benchRuns').mockResolvedValue([{ run: run({ set_id: 'own:mine', set_name: 'My tests', repeats: 2 }), progress: null }])
    vi.spyOn(api, 'benchRun').mockResolvedValue(detail({ run: run({ set_id: 'own:mine', set_name: 'My tests', repeats: 2 }) }))
    await open()
    await click(button('Pick these again'))
    expect($$<HTMLInputElement>('.bench-models input:checked').map((i) => i.getAttribute('aria-label'))).toEqual(['ollama/hf.co/x/qwen:9b', 'ollama/mimo:9b'])
    expect($<HTMLSelectElement>('select[aria-label="Set"]')!.value).toBe('own:mine')
    expect($<HTMLSelectElement>('select[aria-label="Repeats"]')!.value).toBe('2')
    expect($('.bench-size')!.textContent).toBe('2 models × 1 case × 2 repeats = 4 runs')
  })

  it('does not pick the set of an earlier bench when that set cannot be used any more', async () => {
    const gone = run({ set_id: 'own:broken', set_name: 'broken' })
    vi.spyOn(api, 'benchRuns').mockResolvedValue([{ run: gone, progress: null }, { run: run({ id: 'x', set_id: 'own:deleted' }), progress: null }])
    vi.spyOn(api, 'benchRun').mockResolvedValue(detail({ run: gone }))
    await open()
    await click(button('Pick these again'))
    expect($<HTMLSelectElement>('select[aria-label="Set"]')!.value).toBe('starter')
    expect($('.bench-size')!.textContent).toBe('2 models × 2 cases × 2 repeats = 8 runs')
  })

  it('is opened by the Bench button of the sidebar, which stands before Activity', async () => {
    vi.spyOn(api, 'systemStatus').mockResolvedValue({ cpu_temp: 55, cpu_limit: 90, local_models: [], restart_needed: false, stopped: [], page: null })
    await act(async () => { root.render(createElement(Sidebar, { onSettings: () => {} })) })
    expect($('[role="dialog"]')).toBeNull()
    expect($$('.side-buttons button').map((b) => b.textContent)).toEqual(['Findings', 'Caido', 'Bench', 'Activity', 'Knowledge', 'Settings'])
    expect(button('Bench')!.title).toBe('Tests for models: the same cases, scripted tools, a table per model')
    await click(button('Bench'))
    expect($('[role="dialog"]')!.getAttribute('aria-label')).toBe('Bench')
    await click(button('Done'))
    expect($('[role="dialog"]')).toBeNull()
  })

  it('closes with its button', async () => {
    await open()
    await click(button('Done'))
    expect(closed).toHaveBeenCalledTimes(1)
  })

  it('says so when the server cannot be asked, and still shows the form', async () => {
    vi.spyOn(api, 'benchSets').mockRejectedValue(new api.ApiError(0, 'Cannot reach the HunterAI backend. Is it running?'))
    await open()
    expect($('.bench-error')!.textContent).toBe('Cannot reach the HunterAI backend. Is it running?')
    expect(button('Run')!.disabled).toBe(true)
  })
})
