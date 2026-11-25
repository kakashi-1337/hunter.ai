import { describe, expect, it } from 'vitest'
import {
  withAuto, applyEvent, branchLabel, callDurations, canApprove, cardStatus, formatDuration, messageText, pendingAsks, pruneOptions, renderItems,
  appendTail, commandLine, parseArgs, windowStepLine, scanSummary, scanToolLine, citedNumbers, unknownCitations, decisionSummary, keywordsOnlyNote, sourcesNote, sourcesOf, triageFacts, gateLine, parseLines, runningCalls, siblingPosition, stepSummary, stepsFromEvents, toolCards, visible, type ToolCard,
} from '../tree'
import type { Message, PermissionRequest, RunEvent, Step } from '../types'

const msg = (over: Partial<Message>): Message => ({
  id: 'm', chat_id: 'c', parent_id: null, role: 'user', blocks: [], provider: null, model: null,
  fallback_from: null, status: 'complete', created_at: 't', siblings: ['m'], ...over,
})
const user = msg({ id: 'u1', blocks: [{ type: 'text', text: 'hi' }], siblings: ['u1'] })
const started: RunEvent = { type: 'run_started', run_id: 'r', assistant_message_id: 'a1' }
const run = (events: RunEvent[], start: Message[] = [user]) =>
  events.reduce((acc, e) => applyEvent(acc.messages, e, acc.target, 'u1'), { messages: start, target: null as string | null })

describe('withAuto', () => {
  it('switches auto-approve on next to what the chat already sets', () => {
    expect(withAuto({ risk: { execute: 'ask' }, domains: ['a.test'] }, true)).toEqual({ risk: { execute: 'ask' }, domains: ['a.test'], auto: true })
    expect(withAuto(undefined, true)).toEqual({ auto: true })
  })
  it('switched off, it leaves the policy as it was before: no trace of the switch', () => {
    expect(withAuto({ risk: { execute: 'ask' }, auto: true }, false)).toEqual({ risk: { execute: 'ask' } })
    expect('auto' in withAuto({ auto: true }, false)).toBe(false)
  })
})

describe('applyEvent', () => {
  it('adds a streaming assistant message on run_started', () => {
    const { messages, target } = run([started])
    expect(target).toBe('a1')
    expect(messages.map((m) => [m.id, m.role, m.status, m.parent_id])).toEqual([
      ['u1', 'user', 'complete', null], ['a1', 'assistant', 'streaming', 'u1'],
    ])
  })
  it('does not duplicate a message that is already listed', () => {
    const existing = msg({ id: 'a1', role: 'assistant', status: 'streaming', blocks: [{ type: 'text', text: 'par' }] })
    const { messages } = run([started], [user, existing])
    expect(messages).toHaveLength(2)
    expect(messageText(messages[1])).toBe('')
  })
  it('accumulates text and thinking deltas', () => {
    const { messages } = run([
      started, { type: 'thinking_delta', text: 'hm' }, { type: 'thinking_delta', text: 'm' },
      { type: 'text_delta', text: 'echo: ' }, { type: 'text_delta', text: 'hi' },
    ])
    expect(messages[1].blocks).toEqual([{ type: 'thinking', text: 'hmm' }, { type: 'text', text: 'echo: hi' }])
  })
  it('takes the half answer off the page when the model writes it again in a larger window', () => {
    const { messages } = run([
      started, { type: 'thinking_delta', text: 'plan' }, { type: 'text_delta', text: 'The first thing is the' },
      { type: 'answer_restarted', blocks: [] }, { type: 'text_delta', text: 'The whole answer.' },
    ])
    expect(messages[1].blocks).toEqual([{ type: 'text', text: 'The whole answer.' }])
    expect(messages[1].status).toBe('streaming')
  })
  it('keeps what the run did before the answer that is written again', () => {
    const done: Message['blocks'] = [
      { type: 'tool_call', id: 'c1', name: 'execute', arguments: { command: 'id' } },
      { type: 'tool_result', tool_call_id: 'c1', content: 'uid=1000', is_error: false },
    ]
    const { messages } = run([
      started, { type: 'tool_call', id: 'c1', name: 'execute', arguments: { command: 'id' }, error: null },
      { type: 'tool_finished', tool_call_id: 'c1', content: 'uid=1000', is_error: false, duration_ms: 5 },
      { type: 'text_delta', text: 'You are' }, { type: 'answer_restarted', blocks: done }, { type: 'text_delta', text: 'You are uid 1000.' },
    ])
    expect(messages[1].blocks).toEqual([...done, { type: 'text', text: 'You are uid 1000.' }])
  })
  it('ignores deltas when no run has started', () => {
    const before = [user]
    expect(applyEvent(before, { type: 'text_delta', text: 'x' }, null, 'u1').messages).toBe(before)
    expect(applyEvent(before, { type: 'text_delta', text: 'x' }, 'ghost', 'u1').messages).toBe(before)
  })
  it('replaces the streaming message with the final one and keeps siblings', () => {
    const final = { ...msg({ id: 'a1', role: 'assistant', parent_id: 'u1', blocks: [{ type: 'text', text: 'done' }] }) }
    const { siblings: _s, ...withoutSiblings } = final
    const { messages, target } = run([
      started, { type: 'text_delta', text: 'do' },
      { type: 'run_finished', status: 'done', error: null, totals: { duration_ms: 1, turns: 1, calls: 0, input_tokens: 0, output_tokens: 0 }, message: withoutSiblings },
    ])
    expect(target).toBeNull()
    expect(messageText(messages[1])).toBe('done')
    expect(messages[1].status).toBe('complete')
    expect(messages[1].siblings).toEqual(['a1'])
  })
  it('marks the message with the run status when no final message arrives', () => {
    const { messages } = run([
      started, { type: 'text_delta', text: 'half' },
      { type: 'run_finished', status: 'error', error: 'boom', totals: { duration_ms: 1, turns: 1, calls: 0, input_tokens: 0, output_tokens: 0 }, message: null },
    ])
    expect(messages[1].status).toBe('error')
    expect(messageText(messages[1])).toBe('half')
  })
  it('records the fallback on the streaming message', () => {
    const { messages } = run([started, { type: 'fallback', from_provider: 'abliteration', from_model: 'x', to_provider: 'ollama', to_model: 'q', reason: 'unavailable' }])
    expect([messages[1].provider, messages[1].model, messages[1].fallback_from]).toEqual(['ollama', 'q', 'abliteration/x'])
  })
})

describe('siblingPosition and branchLabel', () => {
  it('reports position among siblings', () => {
    expect(siblingPosition(msg({ id: 'b', siblings: ['a', 'b'] }))).toEqual({ index: 1, count: 2 })
    expect(siblingPosition(msg({ id: 'a', siblings: ['a'] }))).toEqual({ index: 0, count: 1 })
    expect(siblingPosition(msg({ id: 'zz', siblings: ['a', 'b'] }))).toEqual({ index: 0, count: 2 })
  })
  it('labels one-based', () => {
    expect(branchLabel(msg({ id: 'b', siblings: ['a', 'b', 'c'] }))).toBe('2/3')
  })
})

describe('pruneOptions', () => {
  it('drops keys the model does not accept', () => {
    expect(pruneOptions({ temperature: 0.2, effort: 'high', thinking: false }, ['effort', 'thinking'])).toEqual({ effort: 'high', thinking: false })
    expect(pruneOptions({ temperature: 0.2 }, [])).toEqual({})
  })
  it('drops empty values', () => {
    expect(pruneOptions({ temperature: undefined, max_tokens: 10 }, ['temperature', 'max_tokens'])).toEqual({ max_tokens: 10 })
  })
})

describe('formatDuration', () => {
  it('formats ms, seconds and minutes', () => {
    expect(formatDuration(450)).toBe('450ms')
    expect(formatDuration(12_340)).toBe('12.3s')
    expect(formatDuration(125_000)).toBe('2m 5s')
  })
})

describe('stepsFromEvents', () => {
  const step = (id: string, status: string): Step => ({ id, run_id: 'r', seq: 1, kind: 'model_call', name: 'p/m', status, started_at: 't', ended_at: null, input_tokens: 0, output_tokens: 0, detail: {} })
  it('upserts steps by id in arrival order', () => {
    let steps: Step[] = []
    steps = stepsFromEvents(steps, { type: 'step_started', step: step('s1', 'running') })
    steps = stepsFromEvents(steps, { type: 'step_finished', step: step('s2', 'done') })
    steps = stepsFromEvents(steps, { type: 'step_finished', step: step('s1', 'error') })
    expect(steps.map((s) => [s.id, s.status])).toEqual([['s1', 'error'], ['s2', 'done']])
    expect(stepsFromEvents(steps, { type: 'text_delta', text: 'x' })).toBe(steps)
  })
})

describe('tool rounds', () => {
  const call: RunEvent = { type: 'tool_call', id: 'c1', name: 'execute', arguments: { command: 'id' }, error: null }
  const done: RunEvent = { type: 'tool_finished', tool_call_id: 'c1', content: 'uid=1000', is_error: false, duration_ms: 12 }
  const req = { id: 'p1', tool_call_id: 'c1', tool: 'execute', arguments: { command: 'id' }, can_allow_chat: true } as unknown as PermissionRequest

  it('keeps blocks in arrival order across a tool round', () => {
    const { messages } = run([started, { type: 'text_delta', text: 'Check' }, { type: 'text_delta', text: 'ing.' }, call, done,
      { type: 'thinking_delta', text: 'hm' }, { type: 'text_delta', text: 'You are 1000.' }])
    expect(messages[1].blocks.map((b) => b.type)).toEqual(['text', 'tool_call', 'tool_result', 'thinking', 'text'])
    expect(messageText(messages[1])).toBe('Checking.\nYou are 1000.')
    expect(toolCards(messages[1])).toEqual([{ call: messages[1].blocks[1], result: messages[1].blocks[2] }])
    expect(renderItems(messages[1]).map((i) => i.kind)).toEqual(['text', 'tool', 'thinking', 'text'])
  })
  it('tracks pending asks through replay, answer, and finish', () => {
    const ask: RunEvent = { type: 'permission_request', request: req }
    expect(pendingAsks(pendingAsks([], ask), ask)).toEqual([req])
    expect(pendingAsks([req], { type: 'permission_resolved', request_id: 'p1', tool_call_id: 'c1', answer: 'deny', note: '' })).toEqual([])
    expect(pendingAsks([req], started)).toEqual([])
  })
  it('tracks running calls', () => {
    const on = runningCalls([], { type: 'tool_started', tool_call_id: 'c1', step_id: 's' })
    expect(on).toEqual(['c1'])
    expect(runningCalls(on, done)).toEqual([])
    expect(runningCalls(on, started)).toEqual([])
  })
  it('derives a card status', () => {
    const card = { call: { type: 'tool_call', id: 'c1', name: 'execute', arguments: {} }, result: null } as ToolCard
    expect(cardStatus(card, [req], [])).toBe('waiting')
    expect(cardStatus(card, [], ['c1'])).toBe('running')
    expect(cardStatus(card, [], [])).toBe('queued')
    expect(cardStatus(card, [], [], true)).toBe('stopped')            // the run ended and this call never got a result
    expect(cardStatus(card, [req], ['c1'], true)).toBe('stopped')
    expect(cardStatus({ ...card, result: { type: 'tool_result', tool_call_id: 'c1', content: 'x', is_error: true } }, [], [])).toBe('error')
    expect(cardStatus({ ...card, result: { type: 'tool_result', tool_call_id: 'c1', content: 'x', is_error: false } }, [req], [])).toBe('done')
  })
  it('keeps tool arguments and results out of Markdown', () => {
    const evil = '<img src=x onerror=alert(1)> **bold** [x](javascript:alert(1))'
    const { messages } = run([started, { ...call, arguments: { command: evil } }, { ...done, content: evil }, { type: 'text_delta', text: 'ok' }])
    const items = renderItems(messages[1])
    expect(items.map((i) => i.kind)).toEqual(['tool', 'text'])
    expect(items.flatMap((i) => (i.kind === 'text' ? [i.text] : []))).toEqual(['ok'])
  })
  it('measures a call from its step', () => {
    const step = (o: object) => ({
      id: 's', kind: 'tool_call', name: 'execute', status: 'done', started_at: '2026-11-13T10:00:00.000Z',
      ended_at: '2026-11-13T10:00:01.250Z', detail: { tool_call_id: 'c1' }, ...o,
    }) as unknown as Step
    expect(callDurations([step({}), step({ kind: 'permission' }), step({ ended_at: null, detail: { tool_call_id: 'c2' } })])).toEqual({ c1: 1250 })
  })
  it('makes invisible characters visible', () => {
    expect(visible('rm \u202E x')).toBe('rm \\u{202E} x')
    expect(visible('a\u200Bb\uFEFF\x1b[0m')).toBe('a\\u{200B}b\\u{FEFF}\\u{1B}[0m')
    expect(visible('line\n\ttab')).toBe('line\n\ttab')
  })
  it('holds the Allow buttons for 800 ms', () => {
    expect(canApprove(1000, 1799)).toBe(false)
    expect(canApprove(1000, 1800)).toBe(true)
  })
})

describe('panel helpers', () => {
  const step = (o: object) => ({ id: 's', kind: 'tool_call', name: 'execute', status: 'done', detail: {}, ...o }) as unknown as Step
  it('parses one entry per line', () => {
    expect(parseLines(' a \n\n b\na ')).toEqual(['a', 'b'])
  })
  it('keeps the tail of process output', () => {
    const out = appendTail('x'.repeat(19999), 'yz')
    expect(out).toHaveLength(20000)
    expect(out.endsWith('yz')).toBe(true)
    expect(appendTail('ab', 'cd', 3)).toBe('bcd')
  })
  it('summarises a step', () => {
    expect(stepSummary(step({ detail: { arguments: { command: 'id' }, exit_code: 0 } }))).toBe('id · exit 0')
    expect(stepSummary(step({ name: 'read', detail: { arguments: { path: '/a' } } }))).toBe('/a')
    expect(stepSummary(step({ name: 'web_fetch', detail: { arguments: { url: 'https://x.test' } } }))).toBe('https://x.test')
    expect(stepSummary(step({ name: 'chunker_x', detail: { arguments: { n: 1 } } }))).toBe('chunker_x')
    expect(stepSummary(step({ kind: 'model_call', name: 'anthropic/x', detail: { context: { results_cut: 3, messages_summarized: 8 } } })))
      .toBe('anthropic/x · made to fit its context: 3 old results cut down, 8 earlier messages summarized')
    expect(stepSummary(step({ kind: 'model_call', name: 'anthropic/x', detail: { context: { results_cut: 0, messages_summarized: 0, summary_used: true } } })))
      .toBe('anthropic/x · made to fit its context: the summary of the earlier messages was used')
    expect(stepSummary(step({ kind: 'permission', detail: { decision: 'ask', answer: 'deny', rule: 'preset' } }))).toBe('ask → deny · preset')
    expect(stepSummary(step({ kind: 'permission', detail: { decision: 'allow', rule: 'chat-risk' } }))).toBe('allow · chat-risk')
  })
})

describe('a step that changed the context window of a local model', () => {
  const step = (detail: object, kind = 'context') => ({ id: 's', kind, name: 'context window 16,384 → 32,768', status: 'done', detail }) as unknown as Step
  it('says why the window was raised', () => {
    expect(windowStepLine(step({ window: { from: 16384, to: 32768, why: 'request' } }))).toBe('The chat did not fit the window it had.')
    expect(windowStepLine(step({ window: { from: 16384, to: 32768, why: 'answer' } }))).toBe('The answer filled the window it had: it is written again in this one.')
  })
  it('says why a raise was taken back, in the words of the call that failed', () => {
    expect(windowStepLine(step({ window: { from: 32768, to: 16384, why: 'failed', error: 'HTTP 500: model requires more system memory' } })))
      .toBe('The model did not answer with the larger window (HTTP 500: model requires more system memory): the one it had is used again.')
    expect(windowStepLine(step({ window: { from: 32768, to: 16384, why: 'failed' } }))).toBe('The model did not answer with the larger window: the one it had is used again.')
  })
  it('is nothing for a step that changed no window', () => {
    expect(windowStepLine(step({ characters: 400 }))).toBeNull()                         // the summary of the earlier messages
    expect(windowStepLine(step({ window: { from: 1, to: 2, why: 'request' } }, 'model_call'))).toBeNull()
  })
})

describe('decision steps', () => {
  const step = (over: Partial<Step>): Step => ({
    id: 's', run_id: 'r', seq: 1, kind: 'decision', name: 'x', status: 'done', started_at: 't', ended_at: 't',
    input_tokens: 0, output_tokens: 0, detail: {}, parent_step_id: null, ...over,
  })
  it('summarises a risk gate, a yes/no, a choice, and a failure', () => {
    expect(decisionSummary(step({ name: 'risk gate', detail: { recommendation: 'confirm', risk_score: 2.1, risk_level: 'Needs review - x' } })))
      .toBe('confirm · risk 2.1 of 3')
    expect(decisionSummary(step({ name: 'risk gate', status: 'error', detail: { failed: true, error: 'connection refused' } })))
      .toBe('gate failed: connection refused (the call kept its decision)')
    expect(decisionSummary(step({ detail: { answers: { worth: { verdict: 'yes', probability: 0.91 } } } }))).toBe('worth: yes (0.91)')
    expect(decisionSummary(step({ detail: { answers: { verdict: { choice: 'recheck', confidence: 0.4, action: 'abstain' }, criteria_met: { verdict: 'no', probability: 0.2 } } } })))
      .toBe('verdict: recheck (0.4, abstain) · criteria_met: no (0.2)')
    expect(decisionSummary(step({ status: 'error', detail: { invalid: true, error: 'no JSON', answers: {} } }))).toBe('no usable answer: no JSON')
  })
  it('says what the checks after a run found, instead of "no usable answer" over a check that worked', () => {
    expect(decisionSummary(step({ name: 'answer check', detail: { checked: 3, unsupported: 1 } }))).toBe('3 statements read · 1 not backed by a tool result')
    expect(decisionSummary(step({ name: 'answer check', detail: { checked: 1, unsupported: 0 } }))).toBe('1 statement read · 0 not backed by a tool result')
    expect(decisionSummary(step({ name: 'answer check', status: 'error', detail: { error: 'no JSON' } }))).toBe('could not be made: no JSON')
    expect(decisionSummary(step({ name: 'goal check', detail: { complete: true, checked: 2, shown: 1 } })))
      .toBe('the answer says complete · 1 of 2 open items shown by a tool result')
    expect(decisionSummary(step({ name: 'goal check', detail: { complete: false } }))).toBe('the answer does not say the work is complete: nothing was marked')
    expect(decisionSummary(step({ name: 'goal check', status: 'error', detail: { complete: null, error: 'no JSON' } }))).toBe('could not be made: no JSON')
  })
  it('gate line for the permission prompt never reads as approval', () => {
    expect(gateLine({ recommendation: 'allow', risk_score: 0.2, risk_level: 'Safe - read-only' })).toBe('Risk gate (advice only): allow · risk 0.2 of 3 (Safe - read-only)')
    expect(gateLine({ recommendation: 'confirm', invalid: true, risk_score: 1.5, risk_level: 'x' })).toBe('Risk gate (advice only): no usable answer, treat as unscreened')
    expect(gateLine({ failed: true, error: 'down' })).toBe('Risk gate (advice only): could not be reached (down)')
    expect(gateLine(null)).toBeNull()
  })
})

describe('triageFacts', () => {
  it('shows the four answers, and only the failure when triage failed', () => {
    expect(triageFacts({ severity: 'high', severity_score: 3.1, false_positive: 0.1, in_scope: 0.9, duplicate: 0.05, model: 'x/y' }))
      .toEqual(['severity: high', 'false positive: 0.1', 'in scope: 0.9', 'duplicate: 0.05'])
    expect(triageFacts({ severity: 'medium', false_positive: 0.5, invalid: true, error: 'no JSON' })).toEqual(['triage failed (no JSON)'])
    expect(triageFacts({})).toEqual([])
  })
})

describe('knowledge sources', () => {
  it('citedNumbers finds bracketed source numbers only', () => {
    expect([...citedNumbers('a [1] b [3][2] c [1, 12] d')].sort((x, y) => x - y)).toEqual([1, 2, 3, 12])
    expect(citedNumbers('arr[0] and x[1] and [link](http://x) and [a1] and [4](http://y)').size).toBe(0)
  })
  it('sourcesOf returns the sources block or null', () => {
    const src = { type: 'sources' as const, query: 'q', items: [], hits: 0, error: null }
    expect(sourcesOf(msg({ blocks: [{ type: 'text', text: 'a' }, src] }))).toBe(src)
    expect(sourcesOf(msg({ blocks: [{ type: 'text', text: 'a' }] }))).toBeNull()
  })
  it('sourcesNote tells a failed search, a full context, and an empty result apart', () => {
    const b = (p: object) => ({ type: 'sources' as const, query: 'q', items: [], hits: 0, error: null, ...p })
    expect(sourcesNote(b({ error: 'embedder busy' }))).toBe('Knowledge search failed: embedder busy')
    expect(sourcesNote(b({ hits: 3 }))).toBe("Knowledge search found 3 chunks, but none fit this chat's context budget.")
    expect(sourcesNote(b({}))).toBe('Knowledge search found nothing for this message.')
    expect(sourcesNote(b({ hits: 1, items: [{ n: 1 }] }))).toBeNull()
    expect(unknownCitations('yes [1], also [7] and [3, 9]', b({ items: [{ n: 1 }, { n: 3 }] }) as never)).toEqual([7, 9])
  })
  it('says when only the keyword index answered, and why, also when it found nothing', () => {
    const b = (p: object) => ({ type: 'sources' as const, query: 'q', items: [], hits: 0, error: null, ...p })
    const why = 'Only the keyword index answered this search: the search by meaning could not be asked (embedder busy).'
    expect(keywordsOnlyNote(b({ keywords_only: 'embedder busy', hits: 1, items: [{ n: 1 }] }) as never)).toBe(why)
    expect(keywordsOnlyNote(b({ keywords_only: null }))).toBeNull()
    expect(keywordsOnlyNote(b({}))).toBeNull()                       // an answer from before the field existed
    expect(sourcesNote(b({ keywords_only: 'embedder busy' }))).toBe(`Knowledge search found nothing for this message. ${why}`)
    expect(sourcesNote(b({ keywords_only: 'embedder busy', hits: 1, items: [{ n: 1 }] }) as never)).toBeNull()
  })
})

describe('command lines', () => {
  it('shows every argument and round-trips through parseArgs', () => {
    expect(commandLine('npx', ['-y', '@scope/pkg@1.2.3'])).toBe('npx -y @scope/pkg@1.2.3')
    expect(commandLine('node', ['/a b/index.js', '', 'x;rm -rf ~'])).toBe('node "/a b/index.js" "" "x;rm -rf ~"')
    expect(parseArgs('-y  "/a b/index.js" plain "q\\"uote"')).toEqual(['-y', '/a b/index.js', 'plain', 'q"uote'])
    expect(parseArgs(commandLine(null, ['/a b/c', '--flag=1']))).toEqual(['/a b/c', '--flag=1'])
  })
})

describe('MCP scan lines', () => {
  const high = { severity: 'high' }, info = { severity: 'info' }
  it('says why a tool is held and counts findings worst first', () => {
    expect(scanToolLine({ state: 'ok', held: false, findings: [] })).toBe('accepted')
    expect(scanToolLine({ state: 'flagged', held: true, findings: [info, high, high] })).toBe('held: the scanner found something · 2 high, 1 info')
    expect(scanToolLine({ state: 'changed', held: true, findings: [] })).toBe('held: its definition changed since you accepted it')
    expect(scanToolLine({ state: 'flagged', held: false, findings: [high] })).toBe('not offered to the model, so not held · 1 high')
  })
  it('sums a server up in one sentence', () => {
    const tools = [{ held: true, findings: [high] }, { held: false, findings: [info] }, { held: false, findings: [] }]
    expect(scanSummary({ tools, held: 1, removed: ['old'], first_sight: true }))
      .toBe('3 tool definitions read · 1 held back from the model · 1 with findings · 1 no longer offered by the server · first time this server was seen')
    expect(scanSummary({ tools: [], held: 0, removed: [], first_sight: false })).toBe('0 tool definitions read · none held back')
  })
})

describe('a message sent while the agent worked', () => {
  it('appears in the answer where it was read, live and after a reload', () => {
    const note = { id: 's', run_id: 'r', seq: 3, kind: 'user_note', name: 'message from you', status: 'done', started_at: 't', ended_at: 't',
      input_tokens: 0, output_tokens: 0, detail: { text: 'skip host b' } } as Step
    const { messages } = run([started, { type: 'tool_call', id: 'c1', name: 'shell', arguments: {}, error: null },
      { type: 'tool_finished', tool_call_id: 'c1', content: 'ok', is_error: false, duration_ms: 1 }, { type: 'step_finished', step: note },
      { type: 'text_delta', text: 'Skipping it.' }])
    expect(messages[1].blocks.map((b) => b.type)).toEqual(['tool_call', 'tool_result', 'owner_note', 'text'])
    expect(renderItems(messages[1]).map((i) => i.kind)).toEqual(['tool', 'note', 'text'])
    expect(renderItems(messages[1])[1]).toEqual({ kind: 'note', text: 'skip host b' })
    // Another kind of step changes nothing in the message.
    const other = run([started, { type: 'step_finished', step: { ...note, kind: 'decision' } }])
    expect(other.messages[1].blocks).toEqual([])
  })
})
