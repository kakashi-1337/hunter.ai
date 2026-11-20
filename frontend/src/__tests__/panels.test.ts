import { describe, expect, it } from 'vitest'
import { compact, goalLine, helpers, answerCheckLine, answerCheckOf, filterFindings, findingsMarkdown, machineLine, renderItems, swarmMembers } from '../tree'
import type { AnswerCheckBlock, FindingRow, Message, Step } from '../types'

const step = (over: Partial<Step>): Step => ({
  id: 's', run_id: 'r', seq: 1, kind: 'tool_call', parent_step_id: null, name: 'swarm', status: 'running',
  started_at: '2026-11-14T10:00:00Z', ended_at: null, input_tokens: 0, output_tokens: 0, detail: {}, ...over,
})

describe('swarmMembers', () => {
  const steps = [
    step({ id: 'call', detail: { tool_call_id: 'c1' } }),
    step({ id: 'a', kind: 'subagent', parent_step_id: 'call', name: 'subagent 1 of 3', status: 'done', ended_at: '2026-11-14T10:00:42Z',
      detail: { brief: 'scan host one', status: 'done', verified: true, verdict: 'accept' } }),
    step({ id: 'a1', kind: 'model_call', parent_step_id: 'a' }), step({ id: 'a2', kind: 'tool_call', parent_step_id: 'a' }),
    step({ id: 'a3', kind: 'model_call', parent_step_id: 'a' }),
    step({ id: 'b', kind: 'subagent', parent_step_id: 'call', name: 'subagent 2 of 3', detail: { brief: 'scan host two' } }),
    step({ id: 'b1', kind: 'model_call', parent_step_id: 'b' }),
    step({ id: 'c', kind: 'subagent', parent_step_id: 'call', name: 'subagent 3 of 3', detail: { brief: 'scan host three' } }),
    step({ id: 'other', kind: 'subagent', parent_step_id: 'elsewhere', detail: { brief: 'not of this call' } }),
  ]
  it('lists the members of one call with what each is doing', () => {
    expect(swarmMembers(steps, 'c1', null)).toEqual([
      { n: 1, brief: 'scan host one', state: 'done', verified: true, verdict: 'accept', ms: 42000, turns: 2, calls: 1 },
      { n: 2, brief: 'scan host two', state: 'running', turns: 1, calls: 0 },
      { n: 3, brief: 'scan host three', state: 'queued', turns: 0, calls: 0 },   // waits for a free place
    ])
    expect(swarmMembers(steps, 'c2', null)).toEqual([])
  })
  it('falls back to the result when the steps of that run are not loaded', () => {
    const content = JSON.stringify({ results: [
      { task: 1, status: 'done', verified: false, verdict: 'unverified', answer: 'port 22 is open' },
      { task: 2, status: 'error', error: 'the model did not answer', verified: false }], note: 'x' })
    expect(swarmMembers([], 'c1', content)).toEqual([
      { n: 1, brief: '', state: 'done', verified: false, verdict: 'unverified', turns: 0, calls: 0, answer: 'port 22 is open' },
      { n: 2, brief: '', state: 'error', verified: false, turns: 0, calls: 0, answer: 'the model did not answer' },
    ])
    expect(swarmMembers([], 'c1', 'not json')).toEqual([])
    expect(swarmMembers([], 'c1', '{"results": "no"}')).toEqual([])
  })
  it('adds the answers of the result to the members read from the steps', () => {
    const content = JSON.stringify({ results: [{ task: 1, status: 'done', verified: true, verdict: 'accept', answer: 'found it' }] })
    expect(swarmMembers(steps, 'c1', content)[0]).toMatchObject({ n: 1, brief: 'scan host one', answer: 'found it', turns: 2 })
  })
})

describe('machineLine', () => {
  it('says the temperature and what is loaded, and warns near the limit', () => {
    expect(machineLine({ cpu_temp: 63.4, cpu_limit: 90, local_models: [] })).toEqual({ text: 'CPU 63°C · no local model loaded', level: '', about: '' })
    expect(machineLine({ cpu_temp: 82, cpu_limit: 90, local_models: [{ name: 'mimo:9b', vram_mb: 9216 }] }))
      .toEqual({ text: 'CPU 82°C · mimo:9b loaded (9.0 GB)', level: 'warn', about: '' })
    expect(machineLine({ cpu_temp: 93, cpu_limit: 90, local_models: null })).toEqual({ text: 'CPU 93°C · Ollama is not running', level: 'bad', about: '' })
    expect(machineLine({ cpu_temp: 93, cpu_limit: null, local_models: [] }).level).toBe('')
    expect(machineLine({ cpu_temp: null, cpu_limit: 90, local_models: [{ name: 'a', vram_mb: 0 }, { name: 'b', vram_mb: 512 }] }).text)
      .toBe('a (0.0 GB), b (0.5 GB) loaded')
  })
})

const finding = (over: Partial<FindingRow>): FindingRow => ({
  id: 'f', target_id: 't', title: 'IDOR', severity: 'high', status: 'open', evidence: '', chat_id: null, step_id: null,
  triage: {}, created_at: '2026-11-14T10:00:00Z', target_name: 'acme', chat_title: null, ...over,
})

describe('findings', () => {
  const list = [
    finding({ id: '1', title: 'Banner', severity: 'info' }),
    finding({ id: '2', title: 'IDOR on /orders', severity: 'critical', status: 'confirmed', evidence: 'GET /orders/2\n```\n200', chat_title: 'orders' }),
    finding({ id: '3', title: 'Open redirect', severity: 'low', status: 'false_positive', target_name: 'bolt', target_id: 'u' }),
  ]
  it('filters by severity, status, target, and text', () => {
    expect(filterFindings(list, {}).map((f) => f.id)).toEqual(['1', '2', '3'])
    expect(filterFindings(list, { severity: 'critical' }).map((f) => f.id)).toEqual(['2'])
    expect(filterFindings(list, { status: 'false_positive' }).map((f) => f.id)).toEqual(['3'])
    expect(filterFindings(list, { target_id: 't', text: 'ORDERS' }).map((f) => f.id)).toEqual(['2'])
    expect(filterFindings(list, { text: 'bolt' }).map((f) => f.id)).toEqual(['3'])
  })
  it('writes a report grouped by target, the worst first, with evidence that cannot break out of its block', () => {
    const md = findingsMarkdown(list)
    expect(md.indexOf('## acme')).toBeLessThan(md.indexOf('## bolt'))
    expect(md.indexOf('IDOR on /orders')).toBeLessThan(md.indexOf('Banner'))
    expect(md).toContain('### [critical] IDOR on /orders\n\n- Status: confirmed\n- Recorded: 2026-11-14\n- Chat: orders\n\n````\nGET /orders/2\n```\n200\n````')
    expect(md).toContain('### [low] Open redirect\n\n- Status: false positive\n- Recorded: 2026-11-14\n')
    expect(findingsMarkdown([])).toBe('# Findings\n\nNone.\n')
  })
  it('keeps a title on one line', () => {
    expect(findingsMarkdown([finding({ title: 'a\n## fake heading' })])).toContain('### [high] a ## fake heading\n')
  })
})

describe('answer check', () => {
  const check = (over: Partial<AnswerCheckBlock>): AnswerCheckBlock => ({ type: 'answer_check', claims: [], checked: 0, supported: 0, model: 'anthropic/haiku', ...over })
  it('sums up what was found, the contradicted first', () => {
    expect(answerCheckLine(check({}))).toEqual({ text: 'Answer check: the answer states nothing about what was run or found.', level: '' })
    expect(answerCheckLine(check({ checked: 3, supported: 3 }))).toEqual({ text: 'Answer check: 3 statements about this run, all shown by its tool results.', level: '' })
    expect(answerCheckLine(check({ checked: 1, claims: [{ claim: 'a', verdict: 'unsupported', evidence: '' }] })))
      .toEqual({ text: 'Answer check: of 1 statement about this run, 1 not shown by any tool result of this run.', level: 'warn' })
    expect(answerCheckLine(check({ checked: 4, supported: 1, claims: [{ claim: 'a', verdict: 'contradicted', evidence: 'closed' }, { claim: 'b', verdict: 'unsupported', evidence: '' }, { claim: 'c', verdict: 'unsupported', evidence: '' }] })))
      .toEqual({ text: 'Answer check: of 4 statements about this run, 1 contradicted by a tool result and 2 not shown by any tool result of this run.', level: 'bad' })
  })
  it('is found on a message and is not one of its rendered pieces', () => {
    const m = { id: 'a', chat_id: 'c', parent_id: null, role: 'assistant', provider: null, model: null, fallback_from: null, status: 'complete', created_at: 't', siblings: ['a'],
      blocks: [{ type: 'text', text: 'Port 80 is open.' }, check({ checked: 1 })] } as Message
    expect(answerCheckOf(m)?.checked).toBe(1)
    expect(renderItems(m)).toEqual([{ kind: 'text', text: 'Port 80 is open.' }])
    expect(answerCheckOf({ ...m, blocks: [] })).toBeNull()
  })
})

describe('goal panel', () => {
  const steps = [
    step({ id: 'd', name: 'delegate', status: 'done', detail: { tool_call_id: 'c0', arguments: { brief: 'read the JS bundle' } } }),
    step({ id: 'call', name: 'swarm', detail: { tool_call_id: 'c1' } }),
    step({ id: 'a', kind: 'subagent', parent_step_id: 'call', name: 'subagent 1 of 2', status: 'done', detail: { brief: 'scan host one', status: 'done', verified: true } }),
    step({ id: 'a1', kind: 'model_call', parent_step_id: 'a' }),
    step({ id: 'b', kind: 'subagent', parent_step_id: 'call', name: 'subagent 2 of 2', detail: { brief: 'scan host two' } }),
    step({ id: 'x', name: 'shell', status: 'done' }),
  ]
  it('lists the subagents of a run, delegate calls and swarm members alike', () => {
    expect(helpers(steps)).toEqual([
      { id: 'd', brief: 'read the JS bundle', state: 'done' },
      { id: 'a', brief: 'scan host one', state: 'done', verified: true },
      { id: 'b', brief: 'scan host two', state: 'queued' },
    ])
    expect(helpers([])).toEqual([])
  })
  it('sums the goal up in a line', () => {
    const goal = { chat_id: 'c', objective: 'Map the API', criteria: [], open_items: ['a', 'b'], done_items: ['z'], updated_at: 't' }
    expect(goalLine(goal, helpers(steps))).toBe('2 open · 1 done · 1 of 3 subagents working')
    expect(goalLine(goal, [])).toBe('2 open · 1 done')
    expect(goalLine({ ...goal, done_items: undefined }, [{ id: 'd', brief: 'x', state: 'done' }])).toBe('2 open · 0 done · 1 subagent finished')
  })
})

describe('compact', () => {
  it('shortens big counts', () => { expect([0, 999, 1000, 45678, 2239599].map(compact)).toEqual(['0', '999', '1.0k', '45.7k', '2.2M']) })
})
