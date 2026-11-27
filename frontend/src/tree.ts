import type { AnswerCheckBlock, Block, ChangesBlock, CheckResult, ColibriServer, FolderCheck, Goal, Helper, FindingRow, LlamaFind, LlamaFindModel, LlamaModel, LlamaServer, LocalModel, Message, Options, PermissionRequest, Policy, ProviderInfo, RunEvent, SourcesBlock, Step, SwarmMember, SystemStatus, WebSourcesBlock } from './types'

export function messageText(m: Message): string {
  return m.blocks.filter((b) => b.type === 'text').map((b) => (b as { text: string }).text).join('\n')
}

/** Blocks stay in arrival order (the engine's rule): a delta extends the last block when it has the same type. */
function appendTo(m: Message, kind: 'text' | 'thinking', text: string): Message {
  const blocks = [...m.blocks]
  const last = blocks[blocks.length - 1]
  if (last && last.type === kind) blocks[blocks.length - 1] = { type: kind, text: last.text + text }
  else blocks.push({ type: kind, text })
  return { ...m, blocks }
}

const push = (m: Message, block: Block): Message => ({ ...m, blocks: [...m.blocks, block] })

const replace = (messages: Message[], id: string, f: (m: Message) => Message) =>
  messages.map((m) => (m.id === id ? f(m) : m))

/**
 * Fold one run event into the visible message list.
 * `target` is the assistant message the run is writing to (null when no run is active).
 */
export function applyEvent(
  messages: Message[], e: RunEvent, target: string | null, userMessageId: string | null,
): { messages: Message[]; target: string | null } {
  if (e.type === 'run_started') {
    const id = e.assistant_message_id
    const fresh: Message = {
      id, chat_id: messages[0]?.chat_id ?? '', parent_id: userMessageId, role: 'assistant', blocks: [],
      provider: null, model: null, fallback_from: null, status: 'streaming', created_at: new Date().toISOString(),
      siblings: [id],
    }
    // The server replays a run from the start, so an already-listed message restarts empty.
    const exists = messages.some((m) => m.id === id)
    const next = exists
      ? replace(messages, id, (m) => ({ ...m, blocks: [], status: 'streaming' }))
      : [...messages, fresh]
    return { messages: next, target: id }
  }
  if (!target || !messages.some((m) => m.id === target)) return { messages, target }
  switch (e.type) {
    case 'text_delta':
      return { messages: replace(messages, target, (m) => appendTo(m, 'text', e.text)), target }
    case 'thinking_delta':
      return { messages: replace(messages, target, (m) => appendTo(m, 'thinking', e.text)), target }
    case 'tool_call':
      return { messages: replace(messages, target, (m) => push(m, { type: 'tool_call', id: e.id, name: e.name, arguments: e.arguments })), target }
    case 'tool_finished':
      return {
        messages: replace(messages, target, (m) =>
          push(m, { type: 'tool_result', tool_call_id: e.tool_call_id, content: e.content, is_error: e.is_error })),
        target,
      }
    case 'step_finished':
      // The owner's queued message was just handed to the model: show it where it was read.
      if (e.step.kind !== 'user_note' || typeof e.step.detail?.text !== 'string') return { messages, target }
      return { messages: replace(messages, target, (m) => push(m, { type: 'owner_note', text: e.step.detail.text as string })), target }
    case 'answer_restarted':
      // The half answer on the page is not the answer: what the run holds now stands in its place.
      return { messages: replace(messages, target, (m) => ({ ...m, blocks: e.blocks })), target }
    case 'fallback':
      return {
        messages: replace(messages, target, (m) => ({
          ...m, provider: e.to_provider, model: e.to_model, fallback_from: `${e.from_provider}/${e.from_model}`,
        })),
        target,
      }
    case 'run_finished': {
      const status = e.status === 'done' ? 'complete' : e.status === 'running' ? 'streaming' : e.status
      return {
        messages: replace(messages, target, (m) =>
          e.message ? { ...e.message, siblings: m.siblings } : { ...m, status: status as Message['status'] }),
        target: null,
      }
    }
    default:
      return { messages, target }
  }
}

export function siblingPosition(m: Message): { index: number; count: number } {
  const i = m.siblings.indexOf(m.id)
  return { index: Math.max(0, i), count: Math.max(1, m.siblings.length) }
}

export function branchLabel(m: Message): string {
  const { index, count } = siblingPosition(m)
  return `${index + 1}/${count}`
}

/** What the owner is told, and agrees to, before auto-approve is switched on for a chat. It says what the permission engine does. */
export const AUTO_APPROVE_WARNING = `Switch auto-approve on for this chat?

HunterAI will stop asking before it writes files, runs commands, or contacts hosts, wherever only the mode or your defaults say "ask". The agent and its subagents then act on their own judgement, and on whatever a page, a file, or a tool result talks them into.

It still asks when a call:
• names a host that is out of scope or not on the target's in-scope list (with no target scope: every host it names)
• touches files that usually hold credentials, or that run later
• addresses this machine or your local network
• is one you set to "ask" for this chat, or one the risk gate wants confirmed

What is denied stays denied, and plan mode still writes and runs nothing. A command can reach hosts it does not name: switch this on only for work you are authorised to do, and watch the run.`

/** The chat's policy with auto-approve switched on or off. Switched off, nothing of the switch is left in it. */
export function withAuto(policy: Policy | null | undefined, on: boolean): Policy {
  const { auto: _, ...rest } = policy ?? {}
  return on ? { ...rest, auto: true } : rest
}

export function pruneOptions(options: Options, allowed: string[]): Options {
  const out: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(options)) {
    if (allowed.includes(k) && v !== undefined && v !== null && (v as unknown) !== '') out[k] = v
  }
  return out as Options
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`
}

export function stepsFromEvents(steps: Step[], e: RunEvent): Step[] {
  if (e.type !== 'step_started' && e.type !== 'step_finished') return steps
  return steps.some((s) => s.id === e.step.id)
    ? steps.map((s) => (s.id === e.step.id ? e.step : s))
    : [...steps, e.step]
}

export type ToolCallBlock = Extract<Block, { type: 'tool_call' }>
export type ToolResultBlock = Extract<Block, { type: 'tool_result' }>
export interface ToolCard { call: ToolCallBlock; result: ToolResultBlock | null }
export type RenderItem =
  | { kind: 'text'; text: string }
  | { kind: 'thinking'; text: string }
  | { kind: 'tool'; card: ToolCard }
  | { kind: 'note'; text: string }
  | { kind: 'notice'; text: string; level: '' | 'warn' }

/** The kinds of notice that tell of something HunterAI did by itself and that went well: drawn without the warning. */
const QUIET_NOTICES = ['window', 'thinking']

/** The message's blocks in order, each tool result folded into its call's card. */
export function renderItems(m: Message): RenderItem[] {
  const results = new Map<string, ToolResultBlock>()
  for (const b of m.blocks) if (b.type === 'tool_result' && !results.has(b.tool_call_id)) results.set(b.tool_call_id, b)
  const out: RenderItem[] = []
  for (const b of m.blocks) {
    if (b.type === 'text') { if (b.text) out.push({ kind: 'text', text: b.text }) }
    else if (b.type === 'thinking') { if (b.text) out.push({ kind: 'thinking', text: b.text }) }
    else if (b.type === 'tool_call') out.push({ kind: 'tool', card: { call: b, result: results.get(b.id) ?? null } })
    else if (b.type === 'owner_note') { if (b.text) out.push({ kind: 'note', text: b.text }) }
    // A context window that was raised, or a turn that was asked again and then answered, is something to know, not
    // something that went wrong.
    else if (b.type === 'notice') { if (b.text) out.push({ kind: 'notice', text: b.text, level: QUIET_NOTICES.includes(b.kind) ? '' : 'warn' }) }
  }
  return out
}

export function toolCards(m: Message): ToolCard[] {
  return renderItems(m).flatMap((i) => (i.kind === 'tool' ? [i.card] : []))
}

/** Permission requests waiting for the owner. A replayed stream does not duplicate them. */
export function pendingAsks(asks: PermissionRequest[], e: RunEvent): PermissionRequest[] {
  if (e.type === 'run_started' || e.type === 'run_finished') return asks.length ? [] : asks
  if (e.type === 'permission_request') return asks.some((a) => a.id === e.request.id) ? asks : [...asks, e.request]
  if (e.type === 'permission_resolved') return asks.filter((a) => a.id !== e.request_id)
  return asks
}

export function runningCalls(ids: string[], e: RunEvent): string[] {
  if (e.type === 'run_started' || e.type === 'run_finished') return ids.length ? [] : ids
  if (e.type === 'tool_started') return ids.includes(e.tool_call_id) ? ids : [...ids, e.tool_call_id]
  if (e.type === 'tool_finished') return ids.filter((i) => i !== e.tool_call_id)
  return ids
}

export type CardStatus = 'waiting' | 'running' | 'done' | 'error' | 'queued' | 'stopped'

/** `over`: the message is no longer being written. A call with no result then never finished (the run was stopped or failed). */
export function cardStatus(card: ToolCard, asks: PermissionRequest[], running: string[], over = false): CardStatus {
  if (card.result) return card.result.is_error ? 'error' : 'done'
  if (over) return 'stopped'
  if (asks.some((a) => a.tool_call_id === card.call.id)) return 'waiting'
  return running.includes(card.call.id) ? 'running' : 'queued'
}

/** Milliseconds each finished tool call took, by tool-call id. */
export function callDurations(steps: Step[]): Record<string, number> {
  const out: Record<string, number> = {}
  for (const s of steps) {
    const id = s.detail?.tool_call_id
    if (s.kind === 'tool_call' && s.ended_at && typeof id === 'string') {
      out[id] = Math.max(0, Date.parse(s.ended_at) - Date.parse(s.started_at))
    }
  }
  return out
}

/**
 * Show control and format characters as \u{XXXX}, so bidi overrides and zero-width characters
 * cannot make a command read differently from what runs.
 */
export function visible(text: string): string {
  return text.replace(/[\p{Cc}\p{Cf}]/gu, (c) =>
    c === '\n' || c === '\t' ? c : `\\u{${c.codePointAt(0)!.toString(16).toUpperCase()}}`)
}

export const APPROVE_DELAY_MS = 800
/** The Allow buttons stay disabled briefly, so a double click cannot land on the next request. */
export const canApprove = (shownAtMs: number, nowMs: number): boolean => nowMs - shownAtMs >= APPROVE_DELAY_MS

export const BEHIND_DELAY_MS = 500
/** A press on the page behind a dialog closes it, but not in the first moment after the dialog appeared: the second
 * press of a double click on the button that opened it lands there, and the dialog would be gone as soon as it came. */
export const pressBehindCloses = (shownAtMs: number, nowMs: number): boolean => nowMs - shownAtMs >= BEHIND_DELAY_MS

/** One entry per line: trimmed, blanks and duplicates dropped. */
export function parseLines(text: string): string[] {
  return [...new Set(text.split('\n').map((l) => l.trim()).filter(Boolean))]
}

/** Process output for display: only the last `max` characters are kept. */
export function appendTail(prev: string, chunk: string, max = 20000): string {
  const all = prev + chunk
  return all.length > max ? all.slice(all.length - max) : all
}

/** A one-line description of a tool or permission step for the trajectory. */
export function stepSummary(step: Step): string {
  const d = step.detail as Record<string, unknown>
  if (step.kind === 'permission') {
    const head = d.answer ? `${String(d.decision)} → ${String(d.answer)}` : String(d.decision ?? '')
    return d.rule ? `${head} · ${String(d.rule)}` : head
  }
  const args = (d.arguments ?? {}) as Record<string, unknown>
  const main = ['command', 'path', 'url', 'query', 'pattern'].map((k) => args[k]).find((v) => typeof v === 'string' && v)
  const what = typeof main === 'string' ? main : step.name
  const fit = fitNote(d.context)
  if (fit) return `${what} · ${fit}`
  return typeof d.exit_code === 'number' ? `${what} · exit ${d.exit_code}` : what
}

/** What was done to a request so that it fits the model's context window, or null when it went out as it was. */
function fitNote(context: unknown): string | null {
  if (!context || typeof context !== 'object') return null
  const c = context as Record<string, unknown>
  if (typeof c.error === 'string') return `could not be made to fit its context (${c.error})`
  const cut = Number(c.results_cut ?? 0), summarized = Number(c.messages_summarized ?? 0)
  const parts = [cut > 0 ? `${cut} old result${cut === 1 ? '' : 's'} cut down` : '', summarized > 0 ? `${summarized} earlier messages summarized` : ''].filter(Boolean)
  if (parts.length === 0 && c.summary_used) parts.push('the summary of the earlier messages was used')
  return parts.length ? `made to fit its context: ${parts.join(', ')}` : null
}

/** Why a step changed the context window of a local model, or null for a step that changed none. */
export function windowStepLine(step: Step): string | null {
  const w = (step.detail as Record<string, unknown>).window as { why?: string; error?: string } | undefined
  if (step.kind !== 'context' || !w || typeof w !== 'object') return null
  if (w.why === 'answer') return 'The answer filled the window it had: it is written again in this one.'
  if (w.why === 'failed') return `The model did not answer with the larger window${w.error ? ` (${w.error})` : ''}: the one it had is used again.`
  return 'The chat did not fit the window it had.'
}

/** One line for a decision step: what the decision model answered. */
export function decisionSummary(step: Step): string {
  const d = step.detail as Record<string, unknown>
  if (step.name === 'answer check' || step.name === 'goal check') {       // the checks after a run: they give no "answers"
    if (d.error) return `could not be made: ${String(d.error)}`
    if (step.name === 'answer check') return `${String(d.checked ?? 0)} ${d.checked === 1 ? 'statement' : 'statements'} read · ${String(d.unsupported ?? 0)} not backed by a tool result`
    if (d.complete !== true) return 'the answer does not say the work is complete: nothing was marked'
    return `the answer says complete · ${String(d.shown ?? 0)} of ${String(d.checked ?? 0)} open items shown by a tool result`
  }
  if (d.failed) return `gate failed: ${String(d.error ?? 'unknown')} (the call kept its decision)`
  if (typeof d.recommendation === 'string') return `${d.recommendation} · risk ${String(d.risk_score)} of 3`
  const answers = (d.answers ?? {}) as Record<string, Record<string, unknown>>
  const parts = Object.entries(answers).map(([id, a]) =>
    typeof a.choice === 'string' ? `${id}: ${a.choice} (${String(a.confidence)}, ${String(a.action)})`
      : typeof a.verdict === 'string' ? `${id}: ${a.verdict} (${String(a.probability)})`
        : `${id}: ${String(a.level ?? '')} (${String(a.score ?? '')})`)
  if (d.invalid || parts.length === 0) return `no usable answer${d.error ? `: ${String(d.error)}` : ''}`
  return parts.join(' · ')
}

/** The risk gate's advice as one line for the permission prompt, or null when it did not run. */
export function gateLine(gate: Record<string, unknown> | null | undefined): string | null {
  if (!gate) return null
  const head = 'Risk gate (advice only): '
  if (gate.failed) return `${head}could not be reached (${String(gate.error ?? 'unknown')})`
  if (gate.invalid) return `${head}no usable answer, treat as unscreened`
  const part = gate.partial ? ' · it saw only part of the arguments' : ''
  return `${head}${String(gate.recommendation)} · risk ${String(gate.risk_score)} of 3 (${String(gate.risk_level)})${part}`
}

/** The triage the decision model stored with a finding, as short facts. A triage that failed
 * shows only that: its placeholder numbers mean nothing. */
export function triageFacts(triage: Record<string, unknown>): string[] {
  if (!triage || Object.keys(triage).length === 0) return []
  if (triage.invalid) return [`triage failed${triage.error ? ` (${String(triage.error)})` : ''}`]
  const names: [string, string][] = [['severity', 'severity'], ['false_positive', 'false positive'], ['in_scope', 'in scope'], ['duplicate', 'duplicate']]
  return names.filter(([k]) => triage[k] !== undefined).map(([k, label]) => `${label}: ${String(triage[k])}`)
}

// ---- knowledge sources under an answer

export function sourcesOf(m: Message): SourcesBlock | null {
  return (m.blocks.find((b) => b.type === 'sources') as SourcesBlock | undefined) ?? null
}

/** What the web search before this answer gave it, when one was made. */
export function webSourcesOf(m: Message): WebSourcesBlock | null {
  return (m.blocks.find((b) => b.type === 'web_sources') as WebSourcesBlock | undefined) ?? null
}

/** Why an answer has no web results to show, or null when it has some. */
export function webSourcesNote(b: WebSourcesBlock): string | null {
  const asked = `Web search for "${b.query}"`
  if (b.error) return `${asked} failed: ${b.error}`
  if (b.items.length) return null
  return b.results > 0 ? `${asked} found ${b.results} results, but none fit this chat's context window.` : `${asked} found nothing.`
}

/** The line of a lookup step in the trajectory: how much of what it found was given to the model. */
export function retrievalLine(name: string, detail: Record<string, unknown>): string {
  const used = Number(detail.used ?? 0)
  if (name === 'web_search') {
    const engines = Array.isArray(detail.engines) ? (detail.engines as string[]).join(', ') : ''
    return `${used} of ${Number(detail.results ?? 0)} results used${engines ? ` · ${engines}` : ''}`
  }
  if (name === 'skills') {
    const shown = Array.isArray(detail.shown) ? (detail.shown as { name: string }[]).map((s) => s.name) : []
    const how = detail.by === 'meaning' ? 'meaning' : 'the words they share with the message'
    return `${shown.length} of ${Number(detail.of ?? 0)} skills named, by ${how}${shown.length ? `: ${shown.join(', ')}` : ''}`
  }
  const half = typeof detail.keywords_only === 'string' && detail.keywords_only ? ` · keyword index only (${detail.keywords_only})` : ''
  return `${used} of ${Number(detail.hits ?? 0)} ${name === 'memory' ? 'notes' : 'chunks'} used${half}`
}

/** What the run of this answer changed in its work folder (coding mode), when anything is to be said about it. */
export function changesOf(m: Message): ChangesBlock | null {
  return (m.blocks.find((b) => b.type === 'changes') as ChangesBlock | undefined) ?? null
}

const filesOf = (n: number) => (n === 1 ? '1 file' : `${n} files`)

/** The line over the list of what a run changed: how much, where, and what became of it. */
export function changesView(b: ChangesBlock): { text: string; level: '' | 'warn'; canUndo: boolean } {
  if (!b.files.length) return { text: b.reason, level: 'warn', canUndo: false }
  const head = `This run changed ${filesOf(b.files.length)} in ${b.folder}.`
  const one = b.files.length === 1
  if (b.undone) return { text: `${head} ${one ? 'It was put back as it was' : 'They were put back as they were'} before it.`, level: '', canUndo: false }
  if (!b.left.length) return { text: head, level: '', canUndo: true }
  // An undo was asked and files were left: the owner's later work is in them. It can be asked again.
  const rest = b.files.length - b.left.length
  const left = b.left.length === 1 ? 'was left as it is' : 'were left as they are'
  const text = one ? `${head} It changed after the run and was left as it is.`
    : rest <= 0 ? `${head} All of them changed after the run and were left as they are.`
      : `${head} ${b.left.length} of them changed after the run and ${left}: ${b.left.join(', ')}. `
        + (rest === 1 ? 'The other is as it was before it.' : 'The others are as they were before it.')
  return { text, level: 'warn', canUndo: true }
}

/** How a run of the check ended when it did not pass. */
const ended = (r: CheckResult) => (r.timed_out ? `it did not end within ${Math.round(r.seconds)} seconds and was stopped`
  : r.exit_code === null ? 'it could not be started' : `exit code ${r.exit_code}`)

/** What a folder's check said before a run and after it, for the line beside the list of what the run changed
 * (the line names the command first). Red when the run broke it, green when the run made it pass. */
export function checkView(c: FolderCheck): { text: string; level: '' | 'ok' | 'warn' | 'bad' } {
  const { before, after } = c
  if (!after) {
    const was = !before ? 'was not run before the run' : before.passed ? 'passed before the run' : `failed before the run (${ended(before)})`
    return { text: `${was}. ${c.note}`.trim(), level: 'warn' }
  }
  if (!before) {
    return after.passed ? { text: 'passes after the run. It was not run before it.', level: '' }
      : { text: `fails after the run (${ended(after)}). It was not run before it.`, level: 'warn' }
  }
  if (after.passed) {
    return before.passed ? { text: 'passed before the run and after it.', level: '' } : { text: 'failed before the run, passes after it.', level: 'ok' }
  }
  return before.passed ? { text: `passed before the run, fails after it (${ended(after)}).`, level: 'bad' }
    : { text: `failed before the run and still fails after it (${ended(after)}).`, level: 'warn' }
}

const whenOf = (s: Step) => (s.detail?.when === 'before' ? 'before the first change' : 'after the run')

/** The check of the folder that is going right now, from the steps of the run that is live. */
export function runningCheck(steps: Step[]): { command: string; text: string } | null {
  const going = [...steps].reverse().find((s) => s.kind === 'check' && !s.ended_at)
  return going ? { command: going.name, text: `Running the check of this folder ${whenOf(going)}: ${going.name}` } : null
}

/** One run of the check in the list of a run's steps. */
export function checkStepLine(step: Step): string {
  const d = step.detail as { passed?: boolean; exit_code?: number | null; seconds?: number; timed_out?: boolean }
  if (!step.ended_at) return `${whenOf(step)} · running`
  if (step.status === 'cancelled') return `${whenOf(step)} · stopped with the run`
  if (d.timed_out) return `${whenOf(step)} · stopped: it did not end within ${Math.round(d.seconds ?? 0)} seconds`
  const how = d.passed ? 'passed' : d.exit_code == null ? 'could not be started' : `failed (exit code ${d.exit_code})`
  return `${whenOf(step)} · ${how} · ${d.seconds ?? 0} s`
}

/** What the owner is asked before anything is put back. */
export function undoQuestion(b: ChangesBlock): string {
  const one = b.files.length === 1
  return `Put the ${filesOf(b.files.length)} this run changed in ${b.folder} back as ${one ? 'it was' : 'they were'} before it? `
    + 'A file that changed after the run is left as it is.'
}

export function answerCheckOf(m: Message): AnswerCheckBlock | null {
  return (m.blocks.find((b) => b.type === 'answer_check') as AnswerCheckBlock | undefined) ?? null
}

/** The one line over an answer check: how many statements were looked at and how many are not backed. */
export function answerCheckLine(b: AnswerCheckBlock): { text: string; level: '' | 'warn' | 'bad' } {
  const against = b.claims.filter((c) => c.verdict === 'contradicted').length
  const without = b.claims.length - against
  if (!b.checked) return { text: 'Answer check: the answer states nothing about what was run or found.', level: '' }
  if (!b.claims.length) return { text: `Answer check: ${b.checked} ${b.checked === 1 ? 'statement' : 'statements'} about this run, all shown by its tool results.`, level: '' }
  const parts = [against ? `${against} contradicted by a tool result` : '', without ? `${without} not shown by any tool result of this run` : ''].filter(Boolean)
  return { text: `Answer check: of ${b.checked} ${b.checked === 1 ? 'statement' : 'statements'} about this run, ${parts.join(' and ')}.`, level: against ? 'bad' : 'warn' }
}

/** Source numbers the answer cites: integers inside square brackets that hold only integers and commas. */
export function citedNumbers(text: string): Set<number> {
  const out = new Set<number>()
  for (const m of text.matchAll(/(?<!\w)\[(\d+(?:\s*,\s*\d+)*)\](?!\()/g)) {
    for (const n of m[1].split(',')) if (Number(n) > 0) out.add(Number(n))
  }
  return out
}

/** That the keyword index answered a search alone, and why the other half did not; null when both answered. */
export function keywordsOnlyNote(b: SourcesBlock): string | null {
  return b.keywords_only
    ? `Only the keyword index answered this search: the search by meaning could not be asked (${b.keywords_only}).`
    : null
}

/** Why an answer has no sources to show, or null when it has some. */
export function sourcesNote(b: SourcesBlock): string | null {
  if (b.error) return `Knowledge search failed: ${b.error}`
  if (b.items.length) return null
  const half = keywordsOnlyNote(b)
  return (b.hits > 0
    ? `Knowledge search found ${b.hits} chunks, but none fit this chat's context budget.`
    : 'Knowledge search found nothing for this message.') + (half ? ` ${half}` : '')
}

/** Numbers the answer cites that no source carries: the model made those citations up. */
export function unknownCitations(text: string, b: SourcesBlock): number[] {
  const known = new Set(b.items.map((s) => s.n))
  return [...citedNumbers(text)].filter((n) => !known.has(n)).sort((x, y) => x - y)
}

/** A command line as the owner should read it: every argument visible, spaces quoted. */
export function commandLine(command: string | null | undefined, args: string[] = []): string {
  return [...(command ? [command] : []), ...args].map((a) => (/^[A-Za-z0-9_@%+=:,./-]+$/.test(a) ? a : JSON.stringify(a))).join(' ')
}

/** Split one line of text into arguments: spaces separate, double quotes group. */
export function parseArgs(text: string): string[] {
  const out: string[] = []
  for (const m of text.matchAll(/"((?:[^"\\]|\\.)*)"|(\S+)/g)) out.push(m[1] !== undefined ? m[1].replace(/\\(.)/g, '$1') : m[2])
  return out
}


const SCAN_STATE: Record<string, string> = {
  ok: 'accepted', flagged: 'held: the scanner found something', new: 'held: new since you accepted this server', changed: 'held: its definition changed since you accepted it',
}

/** One line for a scanned MCP tool: its state and what was found, worst first. */
export function scanToolLine(t: { state: string; held: boolean; findings: { severity: string }[] }): string {
  const count = (sev: string) => t.findings.filter((f) => f.severity === sev).length
  const found = (['high', 'medium', 'info'] as const).map((sev) => [count(sev), sev] as const).filter(([n]) => n > 0).map(([n, sev]) => `${n} ${sev}`).join(', ')
  const state = t.held ? SCAN_STATE[t.state] ?? 'held' : t.state === 'ok' ? 'accepted' : 'not offered to the model, so not held'
  return found ? `${state} · ${found}` : state
}

/** The scan of one server in a sentence. */
export function scanSummary(r: { tools: { held: boolean; findings: { severity: string }[] }[]; held: number; removed: string[]; first_sight: boolean }): string {
  const flagged = r.tools.filter((t) => t.findings.some((f) => f.severity !== 'info')).length
  const parts = [`${r.tools.length} tool definitions read`, r.held ? `${r.held} held back from the model` : 'none held back']
  if (flagged) parts.push(`${flagged} with findings`)
  if (r.removed.length) parts.push(`${r.removed.length} no longer offered by the server`)
  return parts.join(' · ') + (r.first_sight ? ' · first time this server was seen' : '')
}

/** The subagents of one swarm call: from the run's steps while they are loaded, and from the call's result otherwise. */
export function swarmMembers(steps: Step[], callId: string, result: string | null): SwarmMember[] {
  let results: Record<string, unknown>[] = []
  if (result) {
    try {
      const parsed = JSON.parse(result) as { results?: unknown }
      if (Array.isArray(parsed?.results)) results = parsed.results.filter((r) => r && typeof r === 'object') as Record<string, unknown>[]
    } catch { /* an error text, not a result */ }
  }
  const answerOf = (n: number): string | undefined => {
    const r = results.find((x) => x.task === n)
    const text = r && (typeof r.answer === 'string' && r.answer ? r.answer : typeof r.error === 'string' ? r.error : undefined)
    return text || undefined
  }
  const call = steps.find((s) => s.kind === 'tool_call' && s.detail?.tool_call_id === callId)
  const members = call ? steps.filter((s) => s.kind === 'subagent' && s.parent_step_id === call.id) : []
  if (members.length) {
    return members.map((m, i) => {
      const own = steps.filter((s) => s.parent_step_id === m.id)
      const state = (m.status === 'running' && own.length === 0 ? 'queued' : m.status === 'done' && m.detail?.status === 'error' ? 'error' : m.status) as SwarmMember['state']
      const out: SwarmMember = {
        n: i + 1, brief: typeof m.detail?.brief === 'string' ? m.detail.brief : '', state,
        turns: own.filter((s) => s.kind === 'model_call').length, calls: own.filter((s) => s.kind === 'tool_call').length,
      }
      if (typeof m.detail?.verified === 'boolean') out.verified = m.detail.verified
      if (typeof m.detail?.verdict === 'string') out.verdict = m.detail.verdict
      if (m.ended_at) out.ms = Math.max(0, Date.parse(m.ended_at) - Date.parse(m.started_at))
      const answer = answerOf(i + 1)
      if (answer) out.answer = answer
      return out
    })
  }
  return results.filter((r) => typeof r.task === 'number').map((r) => {
    const out: SwarmMember = { n: r.task as number, brief: '', state: r.status === 'done' ? 'done' : r.status === 'cancelled' ? 'cancelled' : 'error', turns: 0, calls: 0 }
    if (typeof r.verified === 'boolean') out.verified = r.verified
    if (typeof r.verdict === 'string') out.verdict = r.verdict
    const answer = answerOf(out.n)
    if (answer) out.answer = answer
    return out
  })
}

export const NEAR_LIMIT = 10   // degrees under the limit at which the line turns yellow

/** One line about the machine: how hot the processor and the graphics card are, and which local models hold memory. */
const whole = (n: number) => n.toLocaleString('en-US')
type Loaded = NonNullable<SystemStatus['local_models']>[number]
const inMemory = (m: Loaded) => ((m.size_mb ?? m.vram_mb) / 1024).toFixed(1)
/** Partly on the GPU and partly on the processor, in the words of `ollama ps`: "40%/60% CPU/GPU". */
const split = (m: Loaded) => /\/\d+% CPU\/GPU$/.test(m.processor ?? '')
/** "GPU", "CPU", or the share that is on the GPU; "" from a server that does not say. */
function share(m: Loaded): string {
  if (m.processor === '100% GPU') return 'GPU'
  if (m.processor === '100% CPU') return 'CPU'
  const part = /\/(\d+)% CPU\/GPU$/.exec(m.processor ?? '')
  return part ? `${part[1]}% GPU` : ''
}

type Hot = { name: string; short: string; degrees: number; limit: number | null }
/** The parts whose temperature the server read: the processor, and the graphics card where it can be read. */
function heatParts(s: SystemStatus): Hot[] {
  const out: Hot[] = []
  if (s.cpu_temp !== null) out.push({ name: 'processor', short: 'CPU', degrees: s.cpu_temp, limit: s.cpu_limit })
  if (s.gpu_temp != null) out.push({ name: 'graphics card', short: 'GPU', degrees: s.gpu_temp, limit: s.gpu_limit ?? null })
  return out
}
const heatOf = (p: Hot): '' | 'warn' | 'bad' => (p.limit === null ? '' : p.degrees >= p.limit ? 'bad' : p.degrees >= p.limit - NEAR_LIMIT ? 'warn' : '')

/** How hot the machine is against its limits: '' | 'warn' (a part within ten degrees of its limit) | 'bad' (a part
 * at or over it). The worse of the processor and the graphics card. */
export function heat(s: SystemStatus): '' | 'warn' | 'bad' {
  const all = heatParts(s).map(heatOf)
  return all.includes('bad') ? 'bad' : all.includes('warn') ? 'warn' : ''
}

/** Which part is hot and what that means, for the one who points at the line. '' when none is. */
export function heatNote(s: SystemStatus): string {
  const level = heat(s)
  const say = (p: Hot) => (level === 'bad'
    ? `The ${p.name} is at ${Math.round(p.degrees)}°C, at or over its limit of ${p.limit}°C: a local model is not loaded, and embedding waits, until it has cooled.`
    : `The ${p.name} is at ${Math.round(p.degrees)}°C, close to its limit of ${p.limit}°C.`)
  return level ? heatParts(s).filter((p) => heatOf(p) === level).map(say).join(' ') : ''
}

/** What a live run waits for while it waits for the machine to cool, from its steps. Null when it does not wait. */
export function waitingToCool(steps: Step[]): string | null {
  const going = [...steps].reverse().find((s) => s.kind === 'cooling' && !s.ended_at)
  return going ? `Waiting for the machine to cool before the local model is loaded: ${going.name}. The run goes on by itself.` : null
}

/** The line at the bottom of the sidebar: the models that hold memory now, Ollama's, the one a colibri server holds
 * and those a llama-server holds. `about` says of each loaded model how much memory it takes, where Ollama runs it and
 * with which window, and what it means when part of it is on the processor; of colibri's and llama-server's, the
 * context; and when Ollama and llama-server each hold one, that they share the memory. '' when the server does not say. */
export function machineLine(s: SystemStatus): { text: string; level: '' | 'warn' | 'bad'; about: string } {
  const parts: string[] = []
  for (const p of heatParts(s)) parts.push(`${p.short} ${Math.round(p.degrees)}°C`)
  const held = s.colibri ?? null      // absent from a server from before it asked colibri
  const served = s.llamacpp ?? []     // absent from a server from before it asked llama-server; null without one
  const ollama = s.local_models ?? []
  const size = (m: Loaded) => `(${inMemory(m)} GB${share(m) ? `, ${share(m)}` : ''})`
  const loaded = [...ollama.map((m) => ({ name: m.name, how: size(m) })), ...(held ? [{ name: held.model, how: '(colibri)' }] : []),
    ...served.map((m) => ({ name: m.model, how: '(llama.cpp)' }))]
  if (s.local_models === null) parts.push('Ollama is not running')
  if (loaded.length === 1) parts.push(`${loaded[0].name} loaded ${loaded[0].how}`)
  else if (loaded.length) parts.push(`${loaded.map((m) => `${m.name} ${m.how}`).join(', ')} loaded`)
  else if (s.local_models !== null) parts.push('no local model loaded')
  const about = [
    ...(s.local_models ?? []).filter((m) => m.processor).map((m) =>
      `${m.name}: ${inMemory(m)} GB in memory, ${m.processor} as \`ollama ps\` says it${m.context ? `, loaded with a window of ${whole(m.context)} tokens` : ''}.`
      + (split(m) ? ' Part of it is on the processor, which makes it slow. On a machine with a graphics card it did not fit the memory of that card with this window: a smaller window or a smaller model fits.' : '')),
    ...(held ? [`${held.model}: held by colibri${held.context ? `, with a context of ${whole(held.context)} tokens` : ''}.`] : []),
    ...served.map((m) => `${m.model}: held by llama-server${m.context ? `, with a context of ${whole(m.context)} tokens` : ''}.`),
    // Two programs that know nothing of each other on one card: the one that loads second gets what is left of it.
    ...(ollama.length && served.length ? ['Ollama and llama-server each hold a model now and share the memory of this machine: what does not '
      + 'fit the graphics card runs on the processor, which is slow. To have one at a time, unload Ollama\'s '
      + `(${ollama.map((m) => `\`ollama stop ${m.name}\``).join(', ')}) or stop llama-server.`] : []),
  ].join(' ')
  const level = heat(s) || (s.local_models?.some(split) ? 'warn' : '')
  return { text: parts.join(' · '), level, about }
}

/** What the window setting says about the local model in use: what a window costs for it, how much of the window
 * the rules and tools take, how Ollama runs it now; and a warning when the size in the field (`value`) leaves no
 * room for a chat. `grows`: how far a chat's window grows by itself (0: it stays as it is set). A window that grows
 * as far as the room takes is no warning: it is one more load of the model at the start of every chat. */
export function localWindowHint(local: LocalModel | null, value: number, grows = 0): { lines: string[]; warn: string | null } {
  if (!local) return { lines: [], warn: null }
  const most = local.limit ? ` It takes ${whole(local.limit)} tokens at most.` : ''
  const lines = [
    !local.answers ? `Ollama does not answer, so ${local.model} could not be asked what a window costs.`
      : !local.installed ? `${local.model}, the fallback model, is not installed in Ollama (\`ollama pull ${local.model}\`).`
        : local.bytes_per_token ? `With ${local.model}, each 8,192 tokens of window take about ${(local.bytes_per_token * 8192 / 2 ** 30).toFixed(1)} GB of memory beside the model itself.${most}`
          : `What a larger window takes in memory depends on the model: \`ollama ps\` shows it once ${local.model} is loaded.${most}`,
    `HunterAI's rules and tools take about ${whole(local.overhead.tokens)} tokens of the window (${whole(local.overhead.tools)} tools${local.overhead.on_demand ? `, and ${whole(local.overhead.on_demand)} more that are loaded on demand` : ''}).`,
  ]
  if (local.loaded) lines.push(`Ollama runs it ${local.loaded.processor}${local.loaded.context ? ` with a window of ${whole(local.loaded.context)}` : ''} now.`)
  const short = 'Too small here: with the rules and tools, less than 8,192 tokens are left for the chat'
  const fits = !local.limit || local.limit >= local.enough          // the model takes a window that is large enough
  if (value < local.enough && fits && grows >= local.enough) {
    lines.push('With the rules and tools, less than 8,192 tokens are left for a chat here, so the window of every chat grows by itself '
      + `at its start: one more load of the model. Set ${whole(local.enough)} or more to spare new chats that.`)
    return { lines, warn: null }
  }
  const warn = value >= local.enough ? null
    : !fits ? `${short}, and ${local.model} takes ${whole(local.limit!)} at most. Switch off the MCP servers you do not need, or choose a fallback model with a larger window.`
      : `${short}. Set ${whole(local.enough)} or more.`
  return { lines, warn }
}

/** What Settings says under the address of colibri: what answers there, and a warning for what stands in the way
 * of a chat with it. A server that does not answer is said without alarm: it may be off on purpose. `keySet`: a key
 * is stored for it. */
export function colibriHint(server: ColibriServer | null, keySet: boolean): { lines: string[]; warn: string | null } {
  if (!server || server.state === 'off') return { lines: [], warn: null }
  if (server.state === 'down') return { lines: [`Nothing answers at ${server.url}. Start colibri (./start-here.sh in the colibri folder), then press Check.`], warn: null }
  if (server.state === 'key') return { lines: [], warn: `colibri at ${server.url} ${keySet ? 'did not accept the key that is set' : 'asks for a key'}: enter the one it was started with below.` }
  if (server.state !== 'ok') return { lines: [], warn: `colibri: ${server.said}.` }
  const context = server.context ? `a context of ${whole(server.context)} tokens` : 'a context it does not name'
  const lines = [`It serves ${server.model}: ${context}, ${server.images ? 'reads pictures' : 'reads text only'}. Pick it in the model list of a chat.`]
  if (server.overhead != null) lines.push(`HunterAI's rules and tools take about ${whole(server.overhead)} tokens of that context.`)
  const warn = server.enough
    ? `Too small here: with the rules and tools, less than 8,192 tokens are left for the chat. Start colibri with a larger context: ./start-here.sh --ctx ${server.enough} in the colibri folder (a larger context needs more memory).`
    : null
  return { lines, warn }
}

const ROUTER_SHOWN = 6        // the models of a router that are named in its line; the rest are counted

/** What Settings says under the address of llama-server: what answers there, and a warning for what stands in the
 * way of a chat with it. A server that does not answer or still loads its model is said without alarm: it may be off
 * on purpose. The command to start it names the context that leaves room for a chat here; `command`: the whole
 * command for this machine stands below, and the line points at it. `keySet`: a key is stored. */
export function llamaCppHint(server: LlamaServer | null, keySet: boolean, command = false): { lines: string[]; warn: string | null } {
  if (!server || server.state === 'off') return { lines: [], warn: null }
  if (server.state === 'down') return { lines: [`Nothing answers at ${server.url}. Start llama-server ${command ? 'with the command below' : `(llama-server -m your-model.gguf -c ${server.start_with})`}, then press Check.`], warn: null }
  if (server.state === 'loading') return { lines: [`llama-server at ${server.url} is still loading its model. Press Check in a moment.`], warn: null }
  if (server.state === 'key') return { lines: [], warn: `llama-server at ${server.url} ${keySet ? 'did not accept the key that is set' : 'asks for a key'}: enter the one it was started with (--api-key) below.` }
  if (server.state !== 'ok') return { lines: [], warn: `llama.cpp: ${server.said}.` }
  const reads = (m: LlamaModel) => (m.images ? 'reads pictures' : 'reads text only')
  const lines: string[] = []
  if (server.router) {
    const one = (m: LlamaModel) => `${m.id} (${m.context ? `${whole(m.context)} tokens` : 'context not named'}, ${reads(m)}, ${m.failed ? 'did not load' : m.loaded ? 'loaded' : 'not loaded'})`
    const more = server.models.length - ROUTER_SHOWN
    lines.push(`It is a router with ${server.models.length} model${server.models.length === 1 ? '' : 's'} and loads the one a chat asks for: `
      + `${server.models.slice(0, ROUTER_SHOWN).map(one).join('; ')}${more > 0 ? `; and ${more} more` : ''}. Pick one in the model list of a chat.`)
    lines.push(`HunterAI's rules and tools take about ${whole(server.overhead)} tokens of a model's context.`)
  } else {
    const m = server.models[0]
    lines.push(`It serves ${m.id}: ${m.context ? `a context of ${whole(m.context)} tokens` : 'a context it does not name'}, ${reads(m)}. Pick it in the model list of a chat.`)
    lines.push(`HunterAI's rules and tools take about ${whole(server.overhead)} tokens of that context.`)
  }
  const warns: string[] = []
  if (server.tools === false) warns.push("The chat template of this model has no tool calls, so a chat with it cannot use HunterAI's tools: start llama-server with a model whose template has them, or give it one (--chat-template-file).")
  const small = server.models.filter((m) => m.enough)
  if (small.length) warns.push(`Too small here: with the rules and tools, less than 8,192 tokens are left for the chat${server.router ? ` with ${small.map((m) => m.id).join(', ')}` : ''}. `
    + `Start llama-server with a larger context: -c ${small[0].enough} (a larger context needs more memory).`)
  const broke = server.models.filter((m) => m.failed)
  if (broke.length) warns.push(`${broke.map((m) => m.id).join(', ')} did not load: llama-server says why in the terminal it runs in.`)
  return { lines, warn: warns.length ? warns.join(' ') : null }
}

/** What Settings shows to start a llama-server on this machine. lines: what was found. warn: what stands in the way.
 * use: the address a button saves (null: no button). models: Ollama's, to choose one for the command. plain: the
 * command for a GGUF file of the owner's own. after: what to do with the command. */
export interface LlamaFindView { lines: string[]; warn: string | null; use: string | null; models: LlamaFindModel[]; plain: string | null; after: string[] }

const NO_LLAMA_SERVER = "No llama-server was found on this machine: none in the llama.cpp folder of HunterAI's data folder, none on the PATH, "
  + 'none where Unsloth Studio keeps its own, and none beside Ollama. It comes with llama.cpp (github.com/ggml-org/llama.cpp) and with a current '
  + 'Ollama; the README says more ("llama-server on your graphics card"). HunterAI does not install it.'
const NO_CARD = 'This llama-server sees no graphics card: its build has no part for one, or the driver is missing. A model started with it runs on '
  + 'the processor, much slower than on a card. The README says where a build for your card comes from ("llama-server on your graphics card").'

/** The block under the address of llama-server that says how to start one here: shown while no address is saved, and
 * for a saved one at which nothing answers or what answers serves no model. `found`: what the machine has (null: not
 * looked at). `server`: what the saved address said (null: not asked). `saved`: the saved address, '' for none.
 * Null: nothing to show. A llama-server that already answers at the usual address is offered as it is. */
export function llamaFindView(found: LlamaFind | null, server: LlamaServer | null, saved: string): LlamaFindView | null {
  const at = found?.address
  if (!found || !at) return null
  const state = saved ? server?.state : found.answers?.state ?? 'down'
  if (saved && (state === undefined || !['down', 'nomodel', 'odd'].includes(state))) return null
  const nothing = { warn: null, models: [], plain: null, after: [] }
  if (state === 'ok') return { ...nothing, lines: [`A llama-server answers at ${at} and serves ${found.answers!.serves.join(', ')}.`], use: at }
  if (state === 'loading') return { ...nothing, lines: [`A llama-server answers at ${at} and is still loading its model.`], use: at }
  if (state === 'key') return { ...nothing, lines: [`A llama-server answers at ${at} and asks for a key: save the address, then enter the key below.`], use: at }
  const warns = [
    ...(state === 'nomodel' ? [`A llama-server already runs at ${at} without a model to chat with, so the port is taken: stop it first (Ctrl+C in the terminal it runs in).`] : []),
    ...(state === 'odd' ? [`Something that is not llama-server answers at ${at}, so that port is taken: change --port in the command, and save the address with the port you chose.`] : []),
    ...(found.program && found.devices?.length === 0 ? [NO_CARD] : []),
  ]
  const warn = warns.length ? warns.join(' ') : null
  if (!found.program) return { ...nothing, lines: [NO_LLAMA_SERVER], warn, use: null }
  const cards = (found.devices ?? []).map((d) => `${d.name}: ${d.card}, ${whole(d.free_mb)} of ${whole(d.total_mb)} MiB free`)
  const lines = [
    `llama-server is at ${found.program}${found.from_ollama ? ', the one Ollama brings' : ''}.${cards.length ? ` It sees ${cards.join('; ')}.` : ''}`,
    !found.ollama ? 'Ollama does not answer, so the models it has cannot be offered here.'
      : found.models.length ? "Start it with a model Ollama has: llama-server reads Ollama's own file, nothing is copied."
        : 'Ollama has no chat model to offer.',
  ]
  const after = [
    ...(found.held.length ? [`Ollama holds ${found.held.join(', ')} in memory now, and both would share the card. Free it first: ${found.held.map((n) => `ollama stop ${n}`).join(' && ')}`] : []),
    'Run the command in a terminal and leave it open: llama-server holds the model until you stop it there (Ctrl+C).',
    saved ? 'When it says it is listening, press Check.' : `When it says it is listening, press "Use ${at}".`,
  ]
  return { lines, warn, use: saved ? null : at, models: found.models, plain: found.plain, after }
}

/** What stands under the command of the model of Ollama that is picked, or in its place when it has none. */
export function llamaModelNote(m: LlamaFindModel): { lines: string[]; warn: string | null } {
  if (!m.command) return { lines: [], warn: `${m.name} cannot be started this way: ${m.why_not}.` }
  return { lines: [`In the model list of a chat it will be ${m.alias}, under llamacpp; it reads ${m.images ? 'pictures' : 'text only'}.`, ...(m.note ? [m.note] : [])], warn: null }
}

/** The providers that are a server of the owner's own: set up by its address in Settings, not by a key. */
const BY_ADDRESS = ['colibri', 'llamacpp']

/** How a provider stands in the list of models: the heading of its group, and the line that stands there in place
 * of models (null when it has models to offer). */
export function providerGroup(p: ProviderInfo): { label: string; empty: string | null } {
  if (!p.configured) {
    const how = p.name === 'codex' ? 'install the codex CLI' : BY_ADDRESS.includes(p.name) ? 'set its address in Settings' : 'add key in settings'
    return { label: `${p.name} — ${how}`, empty: 'not configured' }
  }
  return { label: p.name, empty: p.models.length ? null : p.note ?? 'no models available' }
}

/** What the options sheet of a chat says in place of controls; null when the model of the chat has options to show.
 * A model that its provider does not list now (its server is not running, its key is gone, it was removed) is said as
 * that, with what the provider says of itself: which options such a model takes is not known, and that is not the
 * same as taking none. */
export function optionsNote(provider: string, model: string, providers: ProviderInfo[]): string | null {
  const from = providers.find((p) => p.name === provider)
  const info = from?.models.find((m) => m.id === model)
  if (info) return info.options.length ? null : 'This model has no adjustable options.'
  const why = from ? providerGroup(from).empty : null
  return `${provider} does not offer ${model} now${why ? ` (${why})` : ''}, so the options it takes are not known. `
    + 'They show again when the model is back in the list; or pick another model for this chat.'
}

const HOW_TO_RESTART = 'Press Ctrl+C where it runs, then start it again (scripts/start.sh).'

/** What the owner is told when the server that answers is not the code on disk. A server that does not say (no
 * `restart_needed` in its status) is from before this page was built: it was updated and not restarted. */
export function restartNotice(status: SystemStatus | null): string | null {
  if (!status || status.restart_needed === false) return null
  return status.restart_needed
    ? `Restart HunterAI: its code on this machine changed after the server was started (an update, or an edit). Until then it runs the old code. ${HOW_TO_RESTART}`
    : `Restart HunterAI: the server that answers is older than this page, so parts of the page will not work. ${HOW_TO_RESTART}`
}

/** The page runs an interface older than the one that is built now: an update, and this tab was open through it.
 * `own` is the script this page runs. Null: nothing to say, or one side cannot tell. */
export function reloadNotice(status: SystemStatus | null, own: string | null): string | null {
  if (!status?.page || !own?.startsWith('/assets/') || status.page === own) return null
  return 'Reload this page: HunterAI was updated after it was opened, and it still shows the interface from before.'
}

/** The parts that were in use and do not run now, for the line above the machine state. Null: nothing to say
 * (also when the server does not answer, or is from before it could tell). */
export function stoppedNotice(status: SystemStatus | null): string | null {
  const stopped = status?.stopped ?? []
  return stopped.length ? `Not working: ${stopped.map((s) => s.label).join(', ')}.` : null
}

export interface FindingFilter { severity?: string; status?: string; target_id?: string; text?: string }

export function filterFindings(list: FindingRow[], f: FindingFilter): FindingRow[] {
  const q = (f.text ?? '').trim().toLowerCase()
  return list.filter((x) => (!f.severity || x.severity === f.severity) && (!f.status || x.status === f.status)
    && (!f.target_id || x.target_id === f.target_id)
    && (!q || `${x.title}\n${x.evidence}\n${x.target_name}\n${x.chat_title ?? ''}`.toLowerCase().includes(q)))
}

const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'info']
const oneLine = (text: string) => text.replace(/\s+/g, ' ').trim()

/** The findings as a Markdown report: one section per target, the worst first. Evidence is
 * untrusted text: it goes in a code block whose fence is longer than any run of backticks in it. */
export function findingsMarkdown(list: FindingRow[]): string {
  if (!list.length) return '# Findings\n\nNone.\n'
  const targets = [...new Set(list.map((f) => f.target_name))].sort((a, b) => a.localeCompare(b))
  const out = ['# Findings', '']
  for (const t of targets) {
    out.push(`## ${oneLine(t)}`, '')
    const own = list.filter((f) => f.target_name === t)
      .sort((a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity) || a.created_at.localeCompare(b.created_at))
    for (const f of own) {
      out.push(`### [${f.severity}] ${oneLine(f.title)}`, '', `- Status: ${f.status.replace('_', ' ')}`, `- Recorded: ${f.created_at.slice(0, 10)}`)
      if (f.chat_title) out.push(`- Chat: ${oneLine(f.chat_title)}`)
      out.push('')
      if (f.evidence.trim()) {
        const longest = Math.max(0, ...(f.evidence.match(/`+/g) ?? []).map((r) => r.length))
        const fence = '`'.repeat(Math.max(3, longest + 1))
        out.push(fence, f.evidence.replace(/\s+$/, ''), fence, '')
      }
    }
  }
  return out.join('\n')
}

/** The subagents of a run, in the order they were started: delegate calls and the members of a swarm. */
export function helpers(steps: Step[]): Helper[] {
  const out: Helper[] = []
  for (const s of steps) {
    const state = (['running', 'done', 'error', 'cancelled'].includes(s.status) ? s.status : 'error') as Helper['state']
    if (s.kind === 'subagent') {
      const started = steps.some((x) => x.parent_step_id === s.id)
      const h: Helper = { id: s.id, brief: typeof s.detail?.brief === 'string' ? s.detail.brief : s.name,
        state: s.status === 'running' && !started ? 'queued' : s.status === 'done' && s.detail?.status === 'error' ? 'error' : state }
      if (typeof s.detail?.verified === 'boolean') h.verified = s.detail.verified
      out.push(h)
    } else if (s.kind === 'tool_call' && s.name === 'delegate') {
      const args = s.detail?.arguments as { brief?: unknown } | undefined
      out.push({ id: s.id, brief: typeof args?.brief === 'string' ? args.brief : 'delegated task', state })
    }
  }
  return out
}

/** The one line over the goal panel: how much is open, done, and in the hands of subagents. */
export function goalLine(goal: Goal, list: Helper[]): string {
  const done = goal.done_items?.length ?? 0
  const working = list.filter((h) => h.state === 'running' || h.state === 'queued').length
  const parts = [`${goal.open_items.length} open`, `${done} done`]
  if (list.length) parts.push(working ? `${working} of ${list.length} subagents working` : `${list.length} ${list.length === 1 ? 'subagent' : 'subagents'} finished`)
  return parts.join(' · ')
}

/** What stands beside an item of the goal record: what the check found, in a few words, and what it rests on.
 * null: an open item nobody has said is done. An item that was marked done before items were checked has no mark,
 * and is shown as what it is: the agent's word. */
export function goalMark(goal: Goal, item: string, done: boolean): { label: string; level: 'ok' | 'warn' | 'bad' | ''; detail: string } | null {
  const mark = goal.marks?.[item]
  if (!mark) return done ? { label: 'the agent\'s word', level: '', detail: 'Marked before items were checked: no tool result was read for it.' } : null
  const quoted = `${mark.tool || 'a tool result'}: “${mark.evidence}”`
  if (mark.verdict === 'shown') return { label: 'shown by a tool result', level: 'ok', detail: quoted }
  if (mark.verdict === 'contradicted') return { label: 'a result says the opposite', level: 'bad', detail: quoted }
  if (mark.verdict === 'unchecked') return { label: 'said done, not checked', level: 'warn', detail: `Could not be checked: ${mark.evidence || 'no verdict came back'}` }
  return { label: 'said done, not shown', level: 'warn', detail: mark.evidence || 'No tool result of this chat shows it.' }
}

/** 1234567 -> "1.2M", 45678 -> "45.7k": token counts at a glance. */
export function compact(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`
  return String(n)
}

/** The line under the Caido panel's filter: how many requests are shown, and how many pictures, fonts, sound
 * and film were left out of the list (they are recorded all the same). */
export function caidoListLine(shown: number, total: number, leftOut: number, now: { busy: boolean; filtered: boolean }): string {
  const media = `${leftOut} media request${leftOut === 1 ? '' : 's'} (images, fonts, audio, video)`
  if (shown) return `Showing the newest ${shown} of ${total}.` + (leftOut ? ` ${media} left out: tick "Media too" to list them.` : '')
  if (now.busy) return ''
  if (leftOut) return `Only media was recorded here: ${media}. Tick "Media too" to list them.`
  if (now.filtered) return 'Nothing matches this filter.'
  return 'Nothing recorded yet. Requests of the browser tools, of web_fetch, of downloads, of caido_send, and of commands sent through Caido appear here.'
}
