import type { BriefResult } from './types'

/** The target form as it is typed: the two scope lists one entry a line, and what a pasted brief left open. */
export interface ScopeForm {
  name: string
  notes: string
  inScope: string
  outScope: string
  unplaced: string[]      // hosts a brief names without a side: shown, saved only once the owner places them
  unsupported: string[]   // patterns no scope entry can say: shown as written, never saved
}

export const emptyScopeForm = (): ScopeForm => ({ name: '', notes: '', inScope: '', outScope: '', unplaced: [], unsupported: [] })

export const scopeLines = (text: string): string[] => text.split('\n').map((x) => x.trim()).filter(Boolean)

/** `text` with the entries of `add` it does not hold yet, one a line. What is there stays as typed. */
export function addLines(text: string, add: string[]): string {
  const have = new Set(scopeLines(text).map((x) => x.toLowerCase()))
  const fresh: string[] = []
  for (const entry of add) {
    const key = entry.trim().toLowerCase()
    if (!key || have.has(key)) continue
    have.add(key)
    fresh.push(entry.trim())
  }
  if (!fresh.length) return text
  const kept = text.replace(/\s+$/, '')
  return (kept ? kept + '\n' : '') + fresh.join('\n')
}

const union = (a: string[], b: string[]): string[] => [...new Set([...a, ...b])]

/** The form after a brief was read into it: it adds, it never takes away what the owner typed. */
export function withBrief(form: ScopeForm, brief: BriefResult): ScopeForm {
  const inScope = addLines(form.inScope, brief.in)
  const outScope = addLines(form.outScope, brief.out)
  const placed = new Set([...scopeLines(inScope), ...scopeLines(outScope)].map((x) => x.toLowerCase()))
  const notes = !brief.notes || form.notes.includes(brief.notes) ? form.notes
    : form.notes.trim() ? form.notes.replace(/\s+$/, '') + '\n\n' + brief.notes : brief.notes
  return {
    name: form.name.trim() ? form.name : brief.name,
    notes, inScope, outScope,
    unplaced: union(form.unplaced, brief.unplaced).filter((x) => !placed.has(x.toLowerCase())),
    unsupported: union(form.unsupported, brief.unsupported),
  }
}

/** The owner's answer for a host the brief left open. */
export function placeEntry(form: ScopeForm, entry: string, side: 'in' | 'out'): ScopeForm {
  const unplaced = form.unplaced.filter((x) => x !== entry)
  return side === 'in' ? { ...form, unplaced, inScope: addLines(form.inScope, [entry]) }
    : { ...form, unplaced, outScope: addLines(form.outScope, [entry]) }
}

export const scopeOf = (form: ScopeForm): { in: string[]; out: string[] } =>
  ({ in: scopeLines(form.inScope), out: scopeLines(form.outScope) })

export function briefSummary(brief: BriefResult): string {
  const parts = [
    brief.in.length ? `${brief.in.length} in scope` : '',
    brief.out.length ? `${brief.out.length} out of scope` : '',
    brief.unplaced.length ? `${brief.unplaced.length} with no side given` : '',
  ].filter(Boolean)
  if (!parts.length) return 'Found no host this text puts in or out of scope.'
  const listed = parts.length > 1 ? parts.slice(0, -1).join(', ') + ' and ' + parts[parts.length - 1] : parts[0]
  return `Found ${listed}. Check the lists before you save.`
}
