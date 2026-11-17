import type { AnswerCheckBlock, Block, Goal, Helper, FindingRow, Message, Options, PermissionRequest, Policy, RunEvent, SourcesBlock, Step, SwarmMember, SystemStatus } from './types'

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

/** One line for a decision step: what the decision model answered. */
export function decisionSummary(step: Step): string {
  const d = step.detail as Record<string, unknown>
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

/** Why an answer has no sources to show, or null when it has some. */
export function sourcesNote(b: SourcesBlock): string | null {
  if (b.error) return `Knowledge search failed: ${b.error}`
  if (b.items.length) return null
  return b.hits > 0
    ? `Knowledge search found ${b.hits} chunks, but none fit this chat's context budget.`
    : 'Knowledge search found nothing for this message.'
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

const NEAR_LIMIT = 10   // degrees under the limit at which the line turns yellow

/** One line about the machine: how hot the processor is and which local models hold memory. */
export function machineLine(s: SystemStatus): { text: string; level: '' | 'warn' | 'bad' } {
  const parts: string[] = []
  if (s.cpu_temp !== null) parts.push(`CPU ${Math.round(s.cpu_temp)}°C`)
  if (s.local_models === null) parts.push('Ollama is not running')
  else if (!s.local_models.length) parts.push('no local model loaded')
  else {
    const gb = s.local_models.reduce((n, m) => n + m.vram_mb, 0) / 1024
    parts.push(`${s.local_models.map((m) => m.name).join(', ')} loaded (${gb.toFixed(1)} GB)`)
  }
  const level = s.cpu_temp === null || s.cpu_limit === null ? '' : s.cpu_temp >= s.cpu_limit ? 'bad' : s.cpu_temp >= s.cpu_limit - NEAR_LIMIT ? 'warn' : ''
  return { text: parts.join(' · '), level }
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

/** 1234567 -> "1.2M", 45678 -> "45.7k": token counts at a glance. */
export function compact(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`
  return String(n)
}
