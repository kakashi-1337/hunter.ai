import type { Mode, Options, ProviderInfo } from './types'

/** Slash commands of the composer: a line that starts with "/" and a known word changes the chat instead of
 * being sent. Anything else that starts with a slash ("/etc/passwd is readable") is an ordinary message, and so
 * is a line with a space in front of the slash. */
export interface SlashContext {
  busy: boolean
  provider: string
  model: string
  targets: { id: string; name: string }[]
  personas: { id: string; name: string; description: string }[]
  providers: ProviderInfo[]
}

/** One row of the menu. `text` is what the composer holds once the row is picked; `run` says the command is whole then. */
export interface SlashItem { label: string; hint: string; text: string; run: boolean }

export type SlashResult =
  | { kind: 'new' } | { kind: 'stop' } | { kind: 'mode'; mode: Mode } | { kind: 'model'; provider: string; model: string }
  | { kind: 'persona'; id: string | null } | { kind: 'target'; id: string | null } | { kind: 'effort'; effort: Options['effort'] | undefined }
  | { kind: 'refused'; message: string }

interface Choice { value: string; label: string; hint: string; names: string[]; result: SlashResult }
interface Spec {
  name: string
  hint: string
  noun?: string                               // what its argument is called; a command without one takes no argument
  whileBusy?: boolean                         // may run while the agent works
  choices?(ctx: SlashContext): Choice[]
  run?(ctx: SlashContext): SlashResult         // for a command without an argument
  refuse?(ctx: SlashContext): string | null
}

const MODES: Mode[] = ['plan', 'pentesting', 'coding']
const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'] as const
const plain = (value: string, result: SlashResult, hint = ''): Choice => ({ value, label: value, hint, names: [value], result })

const SPECS: Spec[] = [
  { name: 'new', hint: 'a new chat on the same target', whileBusy: true, run: () => ({ kind: 'new' }) },
  { name: 'mode', hint: 'plan, pentesting or coding', noun: 'mode', choices: () => MODES.map((m) => plain(m, { kind: 'mode', mode: m })) },
  {
    name: 'model', hint: 'the model this chat talks to', noun: 'model',
    choices: (ctx) => ctx.providers.filter((p) => p.configured).flatMap((p) => p.models.map((m) => ({
      value: `${p.name}/${m.id}`, label: `${p.name}/${m.id}`, hint: m.label === m.id ? '' : m.label, names: [`${p.name}/${m.id}`, m.id, m.label],
      result: { kind: 'model', provider: p.name, model: m.id } as SlashResult,
    }))),
  },
  {
    name: 'persona', hint: 'who the assistant is for this chat', noun: 'persona',
    choices: (ctx) => [plain('none', { kind: 'persona', id: null }, 'no persona'),
      ...ctx.personas.map((p) => plain(p.name, { kind: 'persona', id: p.id }, p.description))],
  },
  {
    name: 'target', hint: 'the target this chat works on', noun: 'target',
    choices: (ctx) => [plain('none', { kind: 'target', id: null }, 'a normal chat'), ...ctx.targets.map((t) => plain(t.name, { kind: 'target', id: t.id }))],
  },
  {
    name: 'effort', hint: 'how hard the model thinks', noun: 'effort',
    choices: () => [plain('default', { kind: 'effort', effort: undefined }, "the model's own"), ...EFFORTS.map((e) => plain(e, { kind: 'effort', effort: e }))],
    refuse: (ctx) => ctx.providers.find((p) => p.name === ctx.provider)?.models.find((m) => m.id === ctx.model)?.options.includes('effort')
      ? null : `${ctx.provider}/${ctx.model} has no effort setting.`,
  },
  { name: 'stop', hint: 'stop the run', whileBusy: true, run: () => ({ kind: 'stop' }), refuse: (ctx) => (ctx.busy ? null : 'Nothing is running.') },
]

const listed = (labels: string[], most = 8): string => labels.slice(0, most).join(', ') + (labels.length > most ? ', …' : '')

/** The choices a typed word can mean: the whole name first, then the start of one, then a part of one. */
function meant(choices: Choice[], typed: string): Choice[] {
  const q = typed.trim().toLowerCase()
  const by = (test: (name: string) => boolean) => choices.filter((c) => c.names.some((n) => test(n.toLowerCase())))
  for (const test of [(n: string) => n === q, (n: string) => n.startsWith(q), (n: string) => n.includes(q)]) {
    const found = by(test)
    if (found.length) return found
  }
  return []
}

function parse(text: string): { spec: Spec; arg: string } | null {
  if (!text.startsWith('/') || text.includes('\n')) return null
  const m = /^\/([a-z]+)(?:\s+(.*))?$/i.exec(text.trimEnd())
  const spec = m && SPECS.find((s) => s.name === m[1].toLowerCase())
  return spec ? { spec, arg: (m[2] ?? '').trim() } : null
}

/** What the text does as a command, or null when it is a message to send. */
export function slashCommand(text: string, ctx: SlashContext): SlashResult | null {
  const parsed = parse(text)
  if (!parsed) return null
  const { spec, arg } = parsed
  if (!spec.choices && arg) return null               // "/new endpoint is broken" is a sentence
  if (ctx.busy && !spec.whileBusy) return { kind: 'refused', message: `The agent is working: /stop it or wait, then change the ${spec.noun}.` }
  const refused = spec.refuse?.(ctx)
  if (refused) return { kind: 'refused', message: refused }
  if (!spec.choices) return spec.run!(ctx)
  const choices = spec.choices(ctx)
  if (!arg) return { kind: 'refused', message: `/${spec.name} needs one of: ${listed(choices.map((c) => c.label))}.` }
  const found = meant(choices, arg)
  if (found.length === 1) return found[0].result
  if (!found.length) return { kind: 'refused', message: `No ${spec.noun} matches "${arg}". There is: ${listed(choices.map((c) => c.label))}.` }
  return { kind: 'refused', message: `"${arg}" fits more than one ${spec.noun}: ${listed(found.map((c) => c.label))}.` }
}

/** The rows to offer for the text typed so far, or null when it is not a command being typed. */
export function slashMenu(text: string, ctx: SlashContext): SlashItem[] | null {
  if (!text.startsWith('/') || text.includes('\n')) return null
  if (!/\s/.test(text)) {
    const typed = text.slice(1).toLowerCase()
    if (!/^[a-z]*$/.test(typed)) return null
    const fit = SPECS.filter((s) => s.name.startsWith(typed))
    return fit.length ? fit.map((s) => ({ label: `/${s.name}`, hint: s.hint, text: s.choices ? `/${s.name} ` : `/${s.name}`, run: !s.choices })) : null
  }
  const m = /^\/([a-z]+)\s+(.*)$/i.exec(text)
  const spec = m && SPECS.find((s) => s.name === m[1].toLowerCase())
  if (!spec?.choices) return null
  const choices = spec.choices(ctx)
  const typed = m![2].trim().toLowerCase()
  // While a word is being typed, everything it could still become stays on offer: starts first, then parts.
  const starts = choices.filter((c) => c.names.some((n) => n.toLowerCase().startsWith(typed)))
  const parts = choices.filter((c) => !starts.includes(c) && c.names.some((n) => n.toLowerCase().includes(typed)))
  return [...starts, ...parts].map((c) => ({ label: c.label, hint: c.hint, text: `/${spec.name} ${c.value}`, run: true }))
}
