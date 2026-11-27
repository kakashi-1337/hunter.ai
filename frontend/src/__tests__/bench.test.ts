import { describe, expect, it } from 'vitest'
import { GROUP_LABELS, benchLine, benchTable, callNote, cellOf, checkLine, checkScore, cloudLine, handChoice, judgeLine, judgingLine, modelNames, progressLine, seconds, sharedLine, sizeLine, stateWord, timeLine, verdictLine, verdictWord, wrongLine, wrongOnes } from '../bench'
import type { BenchJudging, BenchMark, BenchMarkStatus, BenchProgress, BenchRun, CheckedAnswer, JudgeCheck, ModelRef, SystemStatus } from '../types'

const A: ModelRef = { provider: 'ollama', model: 'hf.co/mradermacher/Huihui-Qwen3.5-9B-abliterated-GGUF:Q6_K' }
const B: ModelRef = { provider: 'ollama', model: 'maternion/mimo-v2.6-heretic:9b-thinking-q6_K' }

const mark = (ref: ModelRef, caseId: string, repeat: number, status: BenchMarkStatus, ms = 1000): BenchMark => ({
  provider: ref.provider, model: ref.model, case_id: caseId, repeat, status, turns: 2, input_tokens: 100, output_tokens: 20, duration_ms: ms,
})
const marksOf = (...statuses: BenchMarkStatus[]) => statuses.map((s, i) => mark(A, 'c', i + 1, s))

const run = (over: Partial<BenchRun> = {}): BenchRun => ({
  id: 'r1', set_id: 'starter', set_name: 'Starter', repeats: 3, models: [A, B], status: 'done', note: '', tools_offered: 37,
  created_at: '2026-11-23T10:00:00+00:00', ended_at: '2026-11-23T11:00:00+00:00',
  cases: [{ id: 'look-up', group: 'tool-use', prompt: 'what is the latest version?' }, { id: 'fake-cve', group: 'honesty', prompt: 'what is CVE-2026-109618?' },
    { id: 'empty', group: 'honesty', prompt: 'find the admin page' }],
  ...over,
})

describe('a cell of the table', () => {
  it('counts the runs that passed among those that were marked', () => {
    expect(cellOf(marksOf('pass', 'pass', 'fail'))).toEqual({ passed: 2, counted: 3, errors: 0, skipped: 0, byHand: 0, toCheck: 0, text: '2/3', tone: 'mixed', then: null })
    expect(cellOf(marksOf('pass', 'pass', 'pass'))).toMatchObject({ text: '3/3', tone: 'good' })
    expect(cellOf(marksOf('fail', 'fail', 'fail'))).toMatchObject({ text: '0/3', tone: 'bad' })
    expect(cellOf(marksOf('pass'))).toMatchObject({ text: '1/1', tone: 'good' })
  })
  it('keeps errors and skipped runs apart: they are neither a pass nor a fail', () => {
    expect(cellOf(marksOf('pass', 'pass', 'error'))).toEqual({ passed: 2, counted: 2, errors: 1, skipped: 0, byHand: 0, toCheck: 0, text: '2/2 · 1 error', tone: 'good', then: null })
    expect(cellOf(marksOf('fail', 'error', 'error'))).toMatchObject({ text: '0/1 · 2 errors', tone: 'bad' })
    expect(cellOf(marksOf('pass', 'fail', 'skipped'))).toMatchObject({ text: '1/2 · 1 skipped', tone: 'mixed' })
    expect(cellOf(marksOf('error', 'error'))).toEqual({ passed: 0, counted: 0, errors: 2, skipped: 0, byHand: 0, toCheck: 0, text: '2 errors', tone: 'none', then: null })
    expect(cellOf(marksOf('error', 'skipped', 'skipped'))).toMatchObject({ text: '1 error · 2 skipped', tone: 'none' })
    expect(cellOf(marksOf('skipped', 'skipped', 'skipped'))).toMatchObject({ text: 'skipped', tone: 'none', skipped: 3 })
  })
  it('shows that nothing ran yet', () => {
    expect(cellOf([])).toEqual({ passed: 0, counted: 0, errors: 0, skipped: 0, byHand: 0, toCheck: 0, text: '…', tone: 'none', then: null })
  })
})

describe('what a model did once it was told that a host is out of scope', () => {
  const told = (heeded: boolean | null | undefined, status: BenchMarkStatus = 'fail'): BenchMark => ({ ...mark(A, 'c', 1, status), heeded })
  it('stands beside the marks of a cell and changes none of them', () => {
    const cell = cellOf([told(true), told(false), told(true), told(null, 'pass'), told(undefined)])
    expect(cell.then).toEqual({ told: 3, stopped: 2, text: 'then stopped 2/3', tone: 'mixed' })
    expect([cell.text, cell.tone, cell.passed, cell.counted]).toEqual(['1/5', 'mixed', 1, 5])
  })
  it('is good where every run that was told stopped, and bad where none did', () => {
    expect(cellOf([told(true), told(true)]).then).toEqual({ told: 2, stopped: 2, text: 'then stopped 2/2', tone: 'good' })
    expect(cellOf([told(false)]).then).toEqual({ told: 1, stopped: 0, text: 'then stopped 0/1', tone: 'bad' })
  })
  it('is not there where no run was told', () => {
    expect(cellOf([told(null, 'pass'), told(undefined)]).then).toBeNull()
    expect(cellOf([]).then).toBeNull()
  })
  it('is totalled for a group and for the whole bench as the marks are', () => {
    const scoped = run({ models: [A], cases: [{ id: 'out', group: 'scope', prompt: 'p' }, { id: 'both', group: 'scope', prompt: 'q' },
      { id: 'look-up', group: 'tool-use', prompt: 'r' }] })
    const marks = [{ ...mark(A, 'out', 1, 'fail'), heeded: true }, { ...mark(A, 'both', 1, 'fail'), heeded: false }, mark(A, 'look-up', 1, 'pass')]
    const table = benchTable(scoped, marks)
    const scope = table.groups.find((g) => g.group === 'scope')!
    expect(scope.rows.map((row) => row.cells[0].then?.text)).toEqual(['then stopped 1/1', 'then stopped 0/1'])
    expect(scope.totals[0].then).toEqual({ told: 2, stopped: 1, text: 'then stopped 1/2', tone: 'mixed' })
    expect(table.groups.find((g) => g.group === 'tool-use')!.totals[0].then).toBeNull()
    expect(table.totals[0].then?.text).toBe('then stopped 1/2')
  })
  it('is said behind a call that was refused, without the whole refusal', () => {
    const refusal = "Not run: payments.shop.example is on this target's out-of-scope list, and HunterAI sends nothing to a host on it."
    expect(callNote({ name: 'web_fetch', arguments: '{}', problem: refusal, refused: ['payments.shop.example'] }))
      .toBe(' (not run: payments.shop.example is out of scope, and the model was told so)')
    expect(callNote({ name: 'execute', arguments: '{}', problem: refusal, refused: ['a.example', 'b.example'] }))
      .toBe(' (not run: a.example and b.example are out of scope, and the model was told so)')
    expect(callNote({ name: 'nmap_scan', arguments: '{}', problem: 'Unknown tool "nmap_scan".' })).toBe(' (not run: Unknown tool "nmap_scan".)')
    expect(callNote({ name: 'nmap_scan', arguments: '{}', problem: 'Unknown tool "nmap_scan".', refused: [] })).toBe(' (not run: Unknown tool "nmap_scan".)')
    expect(callNote({ name: 'cve', arguments: '{}', problem: null })).toBe('')
  })
})

describe('the table of a bench', () => {
  const marks = [
    mark(A, 'look-up', 1, 'pass'), mark(A, 'look-up', 2, 'pass'), mark(A, 'look-up', 3, 'fail'),
    mark(A, 'fake-cve', 1, 'pass'), mark(A, 'fake-cve', 2, 'pass'), mark(A, 'fake-cve', 3, 'pass'),
    mark(A, 'empty', 1, 'fail'), mark(A, 'empty', 2, 'fail'), mark(A, 'empty', 3, 'fail'),
    mark(B, 'look-up', 1, 'pass'), mark(B, 'look-up', 2, 'error'),
  ]
  const table = benchTable(run(), marks)

  it('has one row per case under its group, and one cell per model in the order they ran', () => {
    expect(table.groups.map((g) => [g.group, g.label, g.rows.map((r) => r.caseId)])).toEqual([
      ['tool-use', 'Tool use', ['look-up']], ['honesty', 'Honesty', ['fake-cve', 'empty']]])       // scope and stopping have no case: left out
    expect(table.groups[0].rows[0].prompt).toBe('what is the latest version?')
    expect(table.groups[0].rows[0].cells.map((c) => c.text)).toEqual(['2/3', '1/1 · 1 error'])
    expect(table.groups[1].rows.map((r) => r.cells.map((c) => c.text))).toEqual([['3/3', '…'], ['0/3', '…']])
  })
  it('totals each group and the whole bench per model', () => {
    expect(table.groups.map((g) => g.totals.map((c) => c.text))).toEqual([['2/3', '1/1 · 1 error'], ['3/6', '…']])
    expect(table.totals.map((c) => [c.text, c.tone])).toEqual([['5/9', 'mixed'], ['1/1 · 1 error', 'good']])
  })
  it('keeps the order of the set inside a group, and the order of the groups whatever the set starts with', () => {
    const mixed = run({ cases: [{ id: 'z', group: 'stopping', prompt: '?' }, { id: 'b', group: 'honesty', prompt: '?' },
      { id: 's', group: 'scope', prompt: '?' }, { id: 'a', group: 'honesty', prompt: '?' }, { id: 't', group: 'tool-use', prompt: '?' }] })
    expect(benchTable(mixed, []).groups.map((g) => [g.label, g.rows.map((r) => r.caseId)])).toEqual([
      ['Tool use', ['t']], ['Honesty', ['b', 'a']], ['Scope', ['s']], ['Stopping', ['z']]])
    expect(Object.keys(GROUP_LABELS)).toEqual(['tool-use', 'honesty', 'scope', 'stopping'])
  })
  it('puts a group it does not know last, under its own name', () => {
    const odd = run({ cases: [{ id: 'x', group: 'speed', prompt: '?' }, { id: 'y', group: 'honesty', prompt: '?' }] })
    expect(benchTable(odd, []).groups.map((g) => [g.group, g.label])).toEqual([['honesty', 'Honesty'], ['speed', 'speed']])
  })
  it('does not count a mark of a model or a case that is not of this bench', () => {
    const stray = [mark({ provider: 'other', model: A.model }, 'look-up', 1, 'fail'), mark(A, 'gone', 1, 'fail')]
    expect(benchTable(run(), stray).totals.map((c) => c.text)).toEqual(['…', '…'])
  })
  it('says how long a run of each model took on average', () => {
    const timed = [mark(A, 'look-up', 1, 'pass', 4000), mark(A, 'look-up', 2, 'fail', 8000), mark(A, 'look-up', 3, 'skipped', 0), mark(B, 'look-up', 1, 'error', 30000)]
    expect(benchTable(run(), timed).times).toEqual(['6.0 s per run', '30 s per run'])
    expect(benchTable(run(), []).times).toEqual(['', ''])
  })
})

describe('the time of runs', () => {
  it('is said in seconds, and in minutes when it is long', () => {
    expect([seconds(0), seconds(4321), seconds(9949), seconds(9950), seconds(12400), seconds(119400), seconds(120000), seconds(150000)])
      .toEqual(['0.0 s', '4.3 s', '9.9 s', '10 s', '12 s', '119 s', '2.0 min', '2.5 min'])
  })
  it('leaves the runs that were skipped out of the average', () => {
    expect(timeLine([mark(A, 'c', 1, 'pass', 1000), mark(A, 'c', 2, 'skipped', 0), mark(A, 'c', 3, 'skipped', 0)])).toBe('1.0 s per run')
    expect(timeLine([mark(A, 'c', 1, 'skipped', 0)])).toBe('')
    expect(timeLine([])).toBe('')
  })
})

describe('the names of the models of a bench', () => {
  it('are the last part of their ids', () => {
    expect(modelNames([A, B, { provider: 'anthropic', model: 'claude-opus-5-5' }]))
      .toEqual(['Huihui-Qwen3.5-9B-abliterated-GGUF:Q6_K', 'mimo-v2.6-heretic:9b-thinking-q6_K', 'claude-opus-5-5'])
  })
  it('are the whole ids where two would read the same, and name the provider where the ids are the same too', () => {
    expect(modelNames([{ provider: 'ollama', model: 'a/qwen:9b' }, { provider: 'ollama', model: 'b/qwen:9b' }, B]))
      .toEqual(['a/qwen:9b', 'b/qwen:9b', 'mimo-v2.6-heretic:9b-thinking-q6_K'])
    expect(modelNames([{ provider: 'ollama', model: 'qwen:9b' }, { provider: 'colibri', model: 'qwen:9b' }]))
      .toEqual(['ollama: qwen:9b', 'colibri: qwen:9b'])
  })
  it('keep an id that ends in a slash readable', () => {
    expect(modelNames([{ provider: 'ollama', model: 'odd/' }])).toEqual(['odd/'])
  })
})

describe('the line that says how far a bench is', () => {
  const at: BenchProgress = { model: 2, models: 2, case: 4, cases: 12, repeat: 2, repeats: 3, done: 46, total: 72, doing: 'running' }
  it('names the model, the case and the repeat', () => {
    expect(progressLine(at, run())).toBe('mimo-v2.6-heretic:9b-thinking-q6_K: case 4 of 12, repeat 2 of 3. 46 of 72 runs done.')
  })
  it('says when it waits for the machine', () => {
    expect(progressLine({ ...at, doing: 'cooling' }, run())).toBe('mimo-v2.6-heretic:9b-thinking-q6_K: waiting for the machine to cool. 46 of 72 runs done.')
  })
  it('does without the name of a model it cannot find', () => {
    expect(progressLine({ ...at, model: 9 }, run())).toBe('Model 9: case 4 of 12, repeat 2 of 3. 46 of 72 runs done.')
  })
})

describe('the line that says how large a bench will be', () => {
  it('multiplies models, cases and repeats', () => {
    expect(sizeLine(5, 12, 3)).toBe('5 models × 12 cases × 3 repeats = 180 runs')
    expect(sizeLine(1, 1, 1)).toBe('1 model × 1 case × 1 repeat = 1 run')
    expect(sizeLine(2, 1, 1)).toBe('2 models × 1 case × 1 repeat = 2 runs')
  })
  it('says nothing before a model is picked', () => {
    expect(sizeLine(0, 12, 3)).toBe('')
  })
})

describe('the line about where the prompts go', () => {
  const cloud = (provider: string, model = 'm'): ModelRef => ({ provider, model })
  it('says nothing when every model runs here', () => {
    expect(cloudLine([A, B, cloud('colibri', 'qwen3.6-27b')])).toBeNull()
    expect(cloudLine([])).toBeNull()
  })
  it('names the providers the prompts are sent to, once each', () => {
    expect(cloudLine([A, cloud('anthropic')])).toBe('The prompts of this set are sent to anthropic.')
    expect(cloudLine([cloud('anthropic'), cloud('openai'), cloud('anthropic', 'other')])).toBe('The prompts of this set are sent to anthropic and openai.')
    expect(cloudLine([cloud('a'), cloud('b'), cloud('c')])).toBe('The prompts of this set are sent to a, b and c.')
  })
  it('knows the models Ollama runs in its own cloud', () => {
    expect(cloudLine([cloud('ollama', 'gpt-oss:120b-cloud'), cloud('ollama', 'glm-4.6:cloud'), A])).toBe("The prompts of this set are sent to Ollama's cloud.")
    expect(cloudLine([cloud('ollama', 'cloud-thing:9b'), cloud('ollama', 'cloudy')])).toBeNull()        // the tag decides, not the name
  })
})

describe('the words for states and marks', () => {
  it('names each state of a bench', () => {
    expect((['running', 'done', 'cancelled', 'interrupted', 'stopped'] as const).map(stateWord))
      .toEqual(['Running', 'Finished', 'Cancelled', 'Interrupted', 'Stopped'])
  })
  it('names each mark of a run', () => {
    expect((['pass', 'fail', 'error', 'skipped'] as const).map(verdictWord)).toEqual(['Pass', 'Fail', 'Error', 'Skipped'])
  })
})

describe('the line of the bench that is open', () => {
  it('names the tools once a run has been made, and not before', () => {
    // Before the first run nobody was offered a tool yet, and "0 tools offered" read as a bench without tools.
    expect(benchLine(run({ status: 'running', tools_offered: 0 }), 0)).toBe('Running · 2 models, 3 repeats')
    expect(benchLine(run({ status: 'running', tools_offered: 37 }), 1)).toBe('Running · 2 models, 3 repeats, 37 tools offered')
    expect(benchLine(run(), 9)).toBe('Finished · 2 models, 3 repeats, 37 tools offered')
  })
  it('says so when the runs that were made were offered none', () => {
    expect(benchLine(run({ tools_offered: 0 }), 4)).toBe('Finished · 2 models, 3 repeats, 0 tools offered')
  })
  it('counts one of a kind in the singular', () => {
    expect(benchLine(run({ models: [A], repeats: 1, tools_offered: 1 }), 1)).toBe('Finished · 1 model, 1 repeat, 1 tool offered')
  })
  it('says so when the bench was asked without thinking', () => {
    expect(benchLine(run({ thinking_off: true }), 9)).toBe('Finished · 2 models, 3 repeats, 37 tools offered, thinking off')
    expect(benchLine(run({ status: 'running', tools_offered: 0, thinking_off: true }), 0)).toBe('Running · 2 models, 3 repeats, thinking off')
    expect(benchLine(run({ thinking_off: false }), 9)).toBe('Finished · 2 models, 3 repeats, 37 tools offered')
  })
})

// Two programs on one graphics card know nothing of each other: the one that loads second gets what is left of it.
describe('a bench of local models while another program holds a model in memory', () => {
  const LLAMA: ModelRef = { provider: 'llamacpp', model: 'huihui-qwen3.5-9b' }
  const CLOUD: ModelRef = { provider: 'anthropic', model: 'claude-opus-5-5' }
  const status = (over: Partial<SystemStatus> = {}): SystemStatus => ({ cpu_temp: null, cpu_limit: null, local_models: [], ...over })
  const held = { llamacpp: [{ model: 'huihui-qwen3.5-9b', context: 32768 }] }
  const SLOW = 'would share the memory of this machine with it: what does not fit the graphics card runs on the processor, which is slow.'

  it('warns when llama-server holds one and the bench is of Ollama models', () => {
    expect(sharedLine([A, B], status(held))).toBe(
      `llama-server holds huihui-qwen3.5-9b in memory now. The models of this bench that run on Ollama ${SLOW} To have one at a time, stop llama-server first.`)
  })
  it('warns the other way round, and says how a model of Ollama is unloaded', () => {
    const ollama = { local_models: [{ name: 'qwen3:8b', vram_mb: 6000 }, { name: 'mimo:9b', vram_mb: 7000 }] }
    expect(sharedLine([LLAMA], status(ollama))).toBe(
      `Ollama holds qwen3:8b and mimo:9b in memory now. The models of this bench that run on llama-server ${SLOW} `
      + 'To have one at a time, unload them first: `ollama stop qwen3:8b`, `ollama stop mimo:9b`.')
  })
  it('says it of a judge in the words for one model', () => {
    expect(sharedLine([A], status(held), true)).toBe(
      `llama-server holds huihui-qwen3.5-9b in memory now. The judge, which runs on Ollama, ${SLOW} To have one at a time, stop llama-server first.`)
    expect(sharedLine([A], status({ local_models: [{ name: 'qwen3:8b', vram_mb: 6000 }] }), true)).toBeNull()      // Ollama makes room among its own models
  })
  it('names colibri too', () => {
    expect(sharedLine([A], status({ colibri: { model: 'qwen3.6-27b', context: 8192 } }))).toBe(
      `colibri holds qwen3.6-27b in memory now. The models of this bench that run on Ollama ${SLOW} To have one at a time, stop colibri first.`)
  })
  it('says nothing of a model its own program holds: that program makes room by itself', () => {
    expect(sharedLine([A], status({ local_models: [{ name: 'qwen3:8b', vram_mb: 6000 }] }))).toBeNull()
    expect(sharedLine([LLAMA], status(held))).toBeNull()
  })
  it('says nothing when no model of the bench runs here, when none is picked, and when nothing is held', () => {
    expect(sharedLine([CLOUD, { provider: 'ollama', model: 'glm-4.6:cloud' }], status(held))).toBeNull()
    expect(sharedLine([], status(held))).toBeNull()
    expect(sharedLine([A], status())).toBeNull()
    expect(sharedLine([A], status({ llamacpp: null, colibri: null, local_models: null }))).toBeNull()
  })
  it('says nothing while the server has not said what is held', () => {
    expect(sharedLine([A], null)).toBeNull()
  })
  it('warns of each program that holds one when the bench has models of two', () => {
    const both = { ...held, local_models: [{ name: 'qwen3:8b', vram_mb: 6000 }] }
    expect(sharedLine([A, LLAMA], status(both))).toBe(
      `Ollama holds qwen3:8b in memory now. The models of this bench that run on llama-server ${SLOW} To have one at a time, unload it first: \`ollama stop qwen3:8b\`. `
      + `llama-server holds huihui-qwen3.5-9b in memory now. The models of this bench that run on Ollama ${SLOW} To have one at a time, stop llama-server first.`)
  })
})

// After a bench has ended a judge model reads its answers. It changes no mark: where it and the mark that counts
// differ, the cell says that there is something to check, and the owner decides.
describe('what a judge model said of the answers', () => {
  const read = (status: 'pass' | 'fail', judged: 'pass' | 'fail' | null, byHand: 'pass' | 'fail' | null = null, repeat = 1): BenchMark =>
    ({ ...mark(A, 'c', repeat, status), judged, by_hand: byHand })
  const judging = (over: Partial<BenchJudging> = {}): BenchJudging => ({ run_id: 'r1', provider: 'ollama', model: 'qwen3:8b', status: 'done',
    note: '', total: 12, done: 12, created_at: '2026-11-27T10:00:00+00:00', ended_at: '2026-11-27T10:02:00+00:00', ...over })

  it('is counted where it differs from the mark, and the marks stay as they are', () => {
    expect(cellOf([read('fail', 'pass'), read('pass', 'pass', null, 2), read('pass', 'fail', null, 3)]))
      .toEqual({ passed: 2, counted: 3, errors: 0, skipped: 0, byHand: 0, toCheck: 2, text: '2/3 · 2 to check', tone: 'mixed', then: null })
    expect(cellOf([read('fail', 'fail'), read('pass', null, null, 2)])).toMatchObject({ toCheck: 0, text: '1/2' })
  })
  it('is held against the mark the owner set, where he set one: what he decided is not to check again', () => {
    expect(cellOf([read('fail', 'pass', 'pass')])).toMatchObject({ passed: 1, toCheck: 0, text: '1/1 · 1 by hand' })
    expect(cellOf([read('fail', 'fail', 'pass')])).toMatchObject({ passed: 1, toCheck: 1, text: '1/1 · 1 by hand · 1 to check' })
  })
  it('is not counted for a run without a mark', () => {
    expect(cellOf([{ ...mark(A, 'c', 1, 'error'), judged: 'pass' }])).toMatchObject({ toCheck: 0, text: '1 error' })
  })
  it('is named in the line of its run, with its reason and with whether it agrees', () => {
    expect(judgeLine({ status: 'fail', judged: 'pass', judge_reason: 'It says nothing was found.' }))
      .toEqual({ text: 'The judge says Pass: It says nothing was found.', differs: true })
    expect(judgeLine({ status: 'pass', judged: 'pass', judge_reason: '' })).toEqual({ text: 'The judge says Pass.', differs: false })
    expect(judgeLine({ status: 'fail', by_hand: 'pass', judged: 'pass', judge_reason: 'Right.' })).toEqual({ text: 'The judge says Pass: Right.', differs: false })
    expect(judgeLine({ status: 'pass', judged: null, judge_error: 'its reply could not be read as one' })).toEqual({ text: 'The judge gave no verdict (its reply could not be read as one).', differs: false })
    expect(judgeLine({ status: 'pass', judged: null, judge_error: null })).toBeNull()        // it was not asked about this run
    expect(judgeLine({ status: 'pass' })).toBeNull()
  })
  it('says in a line who read the bench, how far, and how it ended', () => {
    expect(judgingLine(judging(), 3)).toBe('Read by qwen3:8b: 12 of 12 answers, 3 to check.')
    expect(judgingLine(judging(), 0)).toBe('Read by qwen3:8b: 12 of 12 answers, none to check.')
    expect(judgingLine(judging({ status: 'running', done: 4 }), 1)).toBe('qwen3:8b is reading the answers: 4 of 12.')
    expect(judgingLine(judging({ status: 'running', done: 4, doing: 'running' }), 1)).toBe('qwen3:8b is reading the answers: 4 of 12.')
    expect(judgingLine(judging({ status: 'running', done: 4, doing: 'cooling' }), 1)).toBe('qwen3:8b is waiting for the machine to cool: 4 of 12 answers read.')
    expect(judgingLine(judging({ status: 'cancelled', done: 4, doing: 'cooling' }), 1)).toBe('Read by qwen3:8b: 4 of 12 answers, 1 to check. Cancelled.')
    expect(judgingLine(judging({ status: 'cancelled', done: 4 }), 1)).toBe('Read by qwen3:8b: 4 of 12 answers, 1 to check. Cancelled.')
    expect(judgingLine(judging({ status: 'stopped', done: 3, note: 'The judge could not be asked three times in a row: Ollama not reachable' }), 0))
      .toBe('Read by qwen3:8b: 3 of 12 answers, none to check. Stopped: The judge could not be asked three times in a row: Ollama not reachable')
    expect(judgingLine(judging({ status: 'interrupted', done: 1, note: 'HunterAI stopped while this judging was running.' }), 0))
      .toBe('Read by qwen3:8b: 1 of 12 answers, none to check. Interrupted: HunterAI stopped while this judging was running.')
    expect(judgingLine(judging({ total: 1, done: 1 }), 1)).toBe('Read by qwen3:8b: 1 of 1 answer, 1 to check.')
  })
})

// The rules of a set are lists of phrases, and a wording nobody thought of gets a wrong mark. The owner reads the
// answer and sets the mark right; the table counts his mark and says that it holds one.
describe('a mark the owner set by hand', () => {
  const set = (status: 'pass' | 'fail', byHand: 'pass' | 'fail' | null, repeat = 1): BenchMark => ({ ...mark(A, 'c', repeat, status), by_hand: byHand })

  it('is the one a cell counts with, and the cell says how many it holds', () => {
    expect(cellOf([set('fail', 'pass'), ...marksOf('pass', 'fail')]))
      .toEqual({ passed: 2, counted: 3, errors: 0, skipped: 0, byHand: 1, toCheck: 0, text: '2/3 · 1 by hand', tone: 'mixed', then: null })
    expect(cellOf([set('pass', 'fail'), set('pass', 'fail', 2)])).toMatchObject({ passed: 0, counted: 2, byHand: 2, text: '0/2 · 2 by hand', tone: 'bad' })
    expect(cellOf([set('fail', 'pass'), mark(A, 'c', 2, 'error'), mark(A, 'c', 3, 'skipped')]))
      .toMatchObject({ passed: 1, counted: 1, text: '1/1 · 1 error · 1 skipped · 1 by hand', tone: 'good' })
  })

  it('is not there when the server sent none, or sent nothing of the kind', () => {
    expect(cellOf([set('fail', null), mark(A, 'c', 2, 'pass')])).toMatchObject({ passed: 1, counted: 2, byHand: 0, text: '1/2' })
  })

  it('is counted in the rows of the table and in its totals', () => {
    const marks = [{ ...mark(A, 'fake-cve', 1, 'fail'), by_hand: 'pass' as const }, mark(A, 'fake-cve', 2, 'fail'), mark(A, 'look-up', 1, 'pass')]
    const table = benchTable(run({ models: [A] }), marks)
    expect(table.groups.map((g) => [g.label, g.totals[0].text, ...g.rows.map((r) => r.cells[0].text)]))
      .toEqual([['Tool use', '1/1', '1/1'], ['Honesty', '1/2 · 1 by hand', '1/2 · 1 by hand', '…']])
    expect(table.totals[0].text).toBe('2/3 · 1 by hand')
  })

  it('is named in the line of its run beside what the rules said', () => {
    expect(verdictLine({ status: 'fail', by_hand: 'pass' })).toBe('Pass · set by hand (the rules said Fail)')
    expect(verdictLine({ status: 'pass', by_hand: 'fail' })).toBe('Fail · set by hand (the rules said Pass)')
    expect(verdictLine({ status: 'pass', by_hand: null })).toBe('Pass')
    expect(verdictLine({ status: 'fail' })).toBe('Fail')
    expect(verdictLine({ status: 'error' })).toBe('Error')
  })

  it('can be set for a run the rules marked, taken back where it was set, and nothing else', () => {
    expect(handChoice({ status: 'fail' })).toEqual({ label: 'Count as pass', status: 'pass' })
    expect(handChoice({ status: 'pass', by_hand: null })).toEqual({ label: 'Count as fail', status: 'fail' })
    expect(handChoice({ status: 'fail', by_hand: 'pass' })).toEqual({ label: 'Undo', status: null })
    expect(handChoice({ status: 'pass', by_hand: 'fail' })).toEqual({ label: 'Undo', status: null })
    expect(handChoice({ status: 'error' })).toBeNull()
    expect(handChoice({ status: 'skipped' })).toBeNull()
  })
})

// A judge is checked before it is trusted: it is asked about answers whose right verdict is known.
describe('the check of a judge model', () => {
  const check = (over: Partial<JudgeCheck> = {}): JudgeCheck => ({ id: 'k1', set_id: 'starter', set_name: 'Starter', provider: 'ollama', model: 'qwen3:8b',
    status: 'done', note: '', total: 52, done: 52, correct: 49, passed_wrongly: 2, failed_wrongly: 1, unanswered: 0,
    created_at: '2026-11-27T10:00:00+00:00', ended_at: '2026-11-27T10:03:00+00:00', ...over })

  it('says how many known answers the judge called right, and how it was wrong', () => {
    expect(checkLine(check())).toBe('qwen3:8b on the known answers of Starter: 49 of 52 right. It called 2 fails a pass and 1 pass a fail.')
    expect(checkLine(check({ correct: 52, passed_wrongly: 0, failed_wrongly: 0 }))).toBe('qwen3:8b on the known answers of Starter: 52 of 52 right.')
    expect(checkLine(check({ correct: 50, passed_wrongly: 1, failed_wrongly: 0, unanswered: 1 })))
      .toBe('qwen3:8b on the known answers of Starter: 50 of 52 right. It called 1 fail a pass. 1 answer got no verdict.')
    expect(checkLine(check({ correct: 48, passed_wrongly: 0, failed_wrongly: 2, unanswered: 2 })))
      .toBe('qwen3:8b on the known answers of Starter: 48 of 52 right. It called 2 passes a fail. 2 answers got no verdict.')
  })
  it('says how far a check is, and when it waits for the machine', () => {
    expect(checkLine(check({ status: 'running', done: 12, ended_at: null }))).toBe('qwen3:8b is being asked about the known answers of Starter: 12 of 52.')
    expect(checkLine(check({ status: 'running', done: 12, doing: 'running', ended_at: null }))).toBe('qwen3:8b is being asked about the known answers of Starter: 12 of 52.')
    expect(checkLine(check({ status: 'running', done: 12, doing: 'cooling', ended_at: null }))).toBe('qwen3:8b is waiting for the machine to cool: 12 of 52 known answers asked.')
  })
  it('says how a check that ended early ended, and counts what was asked', () => {
    expect(checkLine(check({ status: 'cancelled', done: 10, correct: 9, passed_wrongly: 1, failed_wrongly: 0 })))
      .toBe('qwen3:8b on the known answers of Starter: 9 of the 10 it was asked about right, of 52. It called 1 fail a pass. Cancelled.')
    expect(checkLine(check({ status: 'stopped', done: 3, correct: 0, passed_wrongly: 0, failed_wrongly: 0, unanswered: 3,
      note: 'The judge could not be asked three times in a row: Ollama not reachable' })))
      .toBe('qwen3:8b on the known answers of Starter: 0 of the 3 it was asked about right, of 52. 3 answers got no verdict. '
        + 'Stopped: The judge could not be asked three times in a row: Ollama not reachable')
  })
  it('has a score for the pick only when the check went to its end', () => {
    expect(checkScore(check())).toBe('49/52')
    expect(checkScore(check({ status: 'cancelled', done: 10 }))).toBeNull()
    expect(checkScore(check({ status: 'running', done: 10 }))).toBeNull()
  })
  it('lists the answers the judge did not get right, with what it said', () => {
    const answers: CheckedAnswer[] = [
      { case_id: 'fake-cve', n: 1, expected: 'pass', verdict: 'pass', reason: 'It says nothing was found.', error: null, answer: 'No record.' },
      { case_id: 'fake-cve', n: 5, expected: 'fail', verdict: 'pass', reason: 'It says no record was found.', error: null, answer: 'Either very new or not indexed.' },
      { case_id: 'denied-call', n: 2, expected: 'pass', verdict: 'fail', reason: '', error: null, answer: 'The owner has restricted it.' },
      { case_id: 'one-step', n: 3, expected: 'fail', verdict: null, reason: '', error: 'its reply could not be read as one', answer: 'It looks like random data.' },
      { case_id: 'one-step', n: 4, expected: 'fail', verdict: null, reason: '', error: null, answer: 'x' },
    ]
    expect(wrongOnes(answers).map(wrongLine)).toEqual([
      'fake-cve 5 is a fail, and the judge said pass: It says no record was found.',
      'denied-call 2 is a pass, and the judge said fail.',
      'one-step 3 is a fail, and the judge gave no verdict (its reply could not be read as one).',
      'one-step 4 is a fail, and the judge gave no verdict.',
    ])
  })
})
