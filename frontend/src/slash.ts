import type { Mode, Options, ProviderInfo } from './types'

/** Slash commands of the composer: a line that starts with "/" and a known word changes the chat instead of
 * being sent, or starts a skill. Anything else that starts with a slash ("/etc/passwd is readable") is an
 * ordinary message, and so is a line with a space in front of the slash. */
export interface SlashContext {
  busy: boolean
  provider: string
  model: string
  targets: { id: string; name: string }[]
  personas: { id: string; name: string; description: string }[]
  providers: ProviderInfo[]
  skills: { name: string; description: string; mode: Mode | null }[]     // the ones switched on
}

/** What /go sends once the chat is back in the mode the work is done in. */
export const GO_TEXT = 'The plan is approved. Carry it out step by step, and stop to tell me when something does not go as planned.'

/** One row of the menu. `text` is what the composer holds once the row is picked; `run` says the command is whole then. */
export interface SlashItem { label: string; hint: string; text: string; run: boolean }

export type SlashResult =
  | { kind: 'new' } | { kind: 'stop' } | { kind: 'mode'; mode: Mode } | { kind: 'model'; provider: string; model: string }
  | { kind: 'persona'; id: string | null } | { kind: 'target'; id: string | null } | { kind: 'effort'; effort: Options['effort'] | undefined }
  | { kind: 'skill'; name: string; mode: Mode | null; text: string }     // the line goes to the model, which gets the skill with it
  | { kind: 'go'; mode: Mode | null }                                     // null: the mode the chat had before the plan
  | { kind: 'skills' }
  | { kind: 'said'; message: string }                                    // the command only says something: /help
  | { kind: 'refused'; message: string }

interface Choice { value: string; label: string; hint: string; names: string[]; result: SlashResult }
interface Spec {
  name: string
  hint: string
  noun?: string                               // what its argument is called; a command without one takes no argument
  waits?: string                              // what has to wait for the run to end, for the message that says so
  optional?: boolean                          // the argument may be left out
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
  {
    name: 'go', hint: 'approve the plan and carry it out', noun: 'mode', waits: 'approve the plan', optional: true,
    run: () => ({ kind: 'go', mode: null }),
    choices: () => MODES.filter((m) => m !== 'plan').map((m) => plain(m, { kind: 'go', mode: m })),
  },
  { name: 'skills', hint: 'the skills that are installed', whileBusy: true, run: () => ({ kind: 'skills' }) },
  { name: 'help', hint: 'the commands there are', whileBusy: true, run: (ctx) => ({ kind: 'said', message: help(ctx) }) },
]

const listed = (labels: string[], most = 8): string => labels.slice(0, most).join(', ') + (labels.length > most ? ', …' : '')

/** What /help says: the commands, and the skills that are switched on. */
function help(ctx: SlashContext): string {
  const skills = ctx.skills.filter((k) => !SPECS.some((s) => s.name === k.name)).map((k) => `/${k.name}`)
  return `The commands: ${SPECS.map((s) => `/${s.name}`).join(', ')}. A lone / lists them with what each does.${skills.length ? ` The skills: ${listed(skills)}.` : ''}`
}

/** How many slips lie between two words: a letter left out, put in or changed, or two neighbours swapped. */
function slips(a: string, b: string): number {
  const d = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array<number>(b.length).fill(0)])
  for (let j = 1; j <= b.length; j++) d[0][j] = j
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1))
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) d[i][j] = Math.min(d[i][j], d[i - 2][j - 2] + 1)
    }
  }
  return d[a.length][b.length]
}

/** The commands and skills that a word which is none of them may have been meant as: a name of four or five letters
 * that is one slip away, a longer one two. A shorter name ("go", "new") is near too many words to guess at. */
function near(word: string, ctx: SlashContext): string[] {
  const names = [...new Set([...SPECS.map((s) => s.name), ...ctx.skills.map((k) => k.name)])]
  return names.filter((name) => name.length > 3 && slips(word, name) <= (name.length > 5 ? 2 : 1))
}

const either = (names: string[]): string => (names.length > 1 ? `${names.slice(0, -1).join(', ')} or ${names[names.length - 1]}` : names[0])

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

/** The skill a line starts, as the server reads it too: a slash, the name in lower case, then a space or the end. */
function skillOf(text: string, ctx: SlashContext): SlashContext['skills'][number] | null {
  const m = /^\/([a-z][a-z0-9-]{0,63})(?=\s|$)/.exec(text)
  return (m && ctx.skills.find((s) => s.name === m[1])) || null
}

/** What the text does as a command, or null when it is a message to send. */
export function slashCommand(text: string, ctx: SlashContext): SlashResult | null {
  const parsed = parse(text)
  if (!parsed) {
    const skill = skillOf(text, ctx)
    if (!skill) {
      // A word that is a slip away from a command was meant as one: sent as it is, it would go to the model as a message.
      const word = text.includes('\n') ? null : /^\/([a-z][a-z0-9-]*)(?=\s|$)/i.exec(text)
      const meant = word ? near(word[1].toLowerCase(), ctx) : []
      return meant.length ? { kind: 'refused', message: `There is no command /${word![1]}. Did you mean ${either(meant.map((n) => `/${n}`))}? `
        + 'A space in front of the slash sends the line as a message.' } : null
    }
    if (ctx.busy) return { kind: 'refused', message: 'The agent is working: /stop it or wait, then start the skill.' }
    return { kind: 'skill', name: skill.name, mode: skill.mode, text }
  }
  const { spec, arg } = parsed
  if (!spec.choices && arg) return null               // "/new endpoint is broken" is a sentence
  if (ctx.busy && !spec.whileBusy) return { kind: 'refused', message: `The agent is working: /stop it or wait, then ${spec.waits ?? `change the ${spec.noun}`}.` }
  const refused = spec.refuse?.(ctx)
  if (refused) return { kind: 'refused', message: refused }
  if (!spec.choices || (spec.optional && !arg)) return spec.run!(ctx)
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
    if (!/^[a-z][a-z0-9-]*$|^$/.test(typed)) return null
    const fit: SlashItem[] = SPECS.filter((s) => s.name.startsWith(typed))
      .map((s) => ({ label: `/${s.name}`, hint: s.hint, text: s.choices && !s.optional ? `/${s.name} ` : `/${s.name}`, run: !s.choices || !!s.optional }))
    // A skill is a command too: picking it leaves room for the task, and Enter then sends the line.
    for (const skill of ctx.skills) {
      if (skill.name.startsWith(typed) && !SPECS.some((s) => s.name === skill.name)) fit.push({ label: `/${skill.name}`, hint: skill.description, text: `/${skill.name} `, run: false })
    }
    return fit.length ? fit : null
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
