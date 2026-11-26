// What the Bench panel shows, worked out from what the server sends. No React here: these are tested as they are.
import type { BenchMark, BenchMarkStatus, BenchProgress, BenchRun, BenchStatus, ModelRef, SystemStatus } from './types'

/** The groups of cases in the order the table shows them. */
export const GROUP_LABELS: Record<string, string> = { 'tool-use': 'Tool use', honesty: 'Honesty', scope: 'Scope', stopping: 'Stopping' }

export interface Cell {
  passed: number
  /** Runs that were marked: passes and fails. An error and a skipped run are neither. */
  counted: number
  errors: number
  skipped: number
  /** Runs whose mark the owner set by hand. They are counted with his mark, and the cell says that it holds them. */
  byHand: number
  text: string
  tone: 'good' | 'mixed' | 'bad' | 'none'
}

const many = (n: number, one: string, more = `${one}s`) => `${n} ${n === 1 ? one : more}`

type Marked = Pick<BenchMark, 'status' | 'by_hand'>

/** The mark a run counts with: the owner's own where he set one, else the one the rules gave. */
export const markOf = (m: Marked): BenchMarkStatus => m.by_hand ?? m.status

/** What a set of runs comes to: "2/3", with errors and skipped runs named beside it. */
export function cellOf(marks: BenchMark[]): Cell {
  const count = (status: BenchMarkStatus) => marks.filter((m) => markOf(m) === status).length
  const passed = count('pass'), errors = count('error'), skipped = count('skipped')
  const counted = passed + count('fail')
  const byHand = marks.filter((m) => m.by_hand != null).length
  const parts = [...(counted ? [`${passed}/${counted}`] : []), ...(errors ? [many(errors, 'error')] : []),
    ...(skipped ? [counted || errors ? `${skipped} skipped` : 'skipped'] : []), ...(byHand ? [`${byHand} by hand`] : [])]
  const tone = !counted ? 'none' : passed === counted ? 'good' : passed === 0 ? 'bad' : 'mixed'
  return { passed, counted, errors, skipped, byHand, text: parts.join(' · ') || '…', tone }
}

/** A time a person reads at a glance: "4.3 s", "42 s", "2.5 min". */
export function seconds(ms: number): string {
  if (ms < 9950) return `${(ms / 1000).toFixed(1)} s`
  if (ms < 120_000) return `${Math.round(ms / 1000)} s`
  return `${(ms / 60_000).toFixed(1)} min`
}

/** How long a run took on average, of the runs that were made; '' when none was. */
export function timeLine(marks: BenchMark[]): string {
  const made = marks.filter((m) => m.status !== 'skipped')
  return made.length ? `${seconds(made.reduce((sum, m) => sum + m.duration_ms, 0) / made.length)} per run` : ''
}

/** The names of the models of a bench as the table heads its columns: the last part of each id, the whole id
 * where two would read the same, and the provider before it where the ids are the same too. */
export function modelNames(models: ModelRef[]): string[] {
  const short = (id: string) => id.slice(id.lastIndexOf('/') + 1) || id
  const twice = (names: string[]) => new Set(names.filter((n, i) => names.indexOf(n) !== i))
  const shorts = models.map((m) => short(m.model))
  const sameShort = twice(shorts), sameId = twice(models.map((m) => m.model))
  return models.map((m, i) => (sameId.has(m.model) ? `${m.provider}: ${m.model}` : sameShort.has(shorts[i]) ? m.model : shorts[i]))
}

export interface BenchTable {
  groups: { group: string; label: string; rows: { caseId: string; prompt: string; cells: Cell[] }[]; totals: Cell[] }[]
  /** One per model, like every row of cells. */
  totals: Cell[]
  times: string[]
}

/** The table of a bench: a row per case under its group, a column per model, totals per group and for the whole. */
export function benchTable(run: BenchRun, marks: BenchMark[]): BenchTable {
  const cases = new Set(run.cases.map((c) => c.id))
  const of = (ref: ModelRef, pick: (m: BenchMark) => boolean) =>
    marks.filter((m) => m.provider === ref.provider && m.model === ref.model && cases.has(m.case_id) && pick(m))
  const order = [...Object.keys(GROUP_LABELS), ...run.cases.map((c) => c.group).filter((g) => !(g in GROUP_LABELS))]
  const groups = [...new Set(order)].map((group) => {
    const own = run.cases.filter((c) => c.group === group)
    const ids = new Set(own.map((c) => c.id))
    return {
      group, label: GROUP_LABELS[group] ?? group,
      rows: own.map((c) => ({ caseId: c.id, prompt: c.prompt, cells: run.models.map((ref) => cellOf(of(ref, (m) => m.case_id === c.id))) })),
      totals: run.models.map((ref) => cellOf(of(ref, (m) => ids.has(m.case_id)))),
    }
  }).filter((g) => g.rows.length)
  return { groups, totals: run.models.map((ref) => cellOf(of(ref, () => true))), times: run.models.map((ref) => timeLine(of(ref, () => true))) }
}

/** Where a running bench is, in a line. */
export function progressLine(p: BenchProgress, run: BenchRun): string {
  const name = modelNames(run.models)[p.model - 1] ?? `Model ${p.model}`
  const at = p.doing === 'cooling' ? 'waiting for the machine to cool' : `case ${p.case} of ${p.cases}, repeat ${p.repeat} of ${p.repeats}`
  return `${name}: ${at}. ${p.done} of ${many(p.total, 'run')} done.`
}

/** How many runs a bench of this size is; '' before a model is picked. */
export function sizeLine(models: number, cases: number, repeats: number): string {
  if (!models) return ''
  return `${many(models, 'model')} × ${many(cases, 'case')} × ${many(repeats, 'repeat')} = ${many(models * cases * repeats, 'run')}`
}

/** A model Ollama runs in its own cloud, not on this machine: its tag says so (gpt-oss:120b-cloud, glm-4.6:cloud). */
const ollamaCloud = (model: string) => /:(.*-)?cloud$/i.test(model)

/** The providers that are a server of the owner's own, at the address he gave: what is sent to them goes to no cloud. */
const OWN_SERVERS = ['colibri', 'llamacpp']

/** Where the prompts of a bench go when not every model runs here; null when all of them do. */
export function cloudLine(models: ModelRef[]): string | null {
  const where = models.map((m) => (OWN_SERVERS.includes(m.provider) ? null : m.provider !== 'ollama' ? m.provider : ollamaCloud(m.model) ? "Ollama's cloud" : null))
  const names = [...new Set(where.filter((w): w is string => w !== null))]
  if (!names.length) return null
  const listed = names.length === 1 ? names[0] : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
  return `The prompts of this set are sent to ${listed}.`
}

/** The programs that run a model on this machine, as a line names them. */
const PROGRAMS: Record<string, string> = { ollama: 'Ollama', llamacpp: 'llama-server', colibri: 'colibri' }

/** The program on this machine that runs this model; null for a model that runs elsewhere. */
const runsOn = (m: ModelRef): string | null =>
  (m.provider === 'ollama' ? (ollamaCloud(m.model) ? null : 'ollama') : OWN_SERVERS.includes(m.provider) ? m.provider : null)

const listed = (names: string[]) => (names.length === 1 ? names[0] : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`)

/** A warning before a bench of local models while another program on this machine holds a model in memory: the two
 * know nothing of each other and share the memory of the machine. A model its own program holds is none: that program makes
 * room by itself. null when there is nothing to warn of, and while the server has not said what is held. */
export function sharedLine(models: ModelRef[], s: SystemStatus | null): string | null {
  if (!s) return null
  const picked = [...new Set(models.map(runsOn).filter((p): p is string => p !== null))]
  const holds: [string, string[]][] = [
    ['ollama', (s.local_models ?? []).map((m) => m.name)],
    ['llamacpp', (s.llamacpp ?? []).map((m) => m.model)],
    ['colibri', s.colibri ? [s.colibri.model] : []],
  ]
  const lines = holds.map(([program, names]) => {
    const others = picked.filter((p) => p !== program).map((p) => PROGRAMS[p])
    if (!names.length || !others.length) return null
    const free = program === 'ollama'
      ? `unload ${names.length === 1 ? 'it' : 'them'} first: ${names.map((n) => `\`ollama stop ${n}\``).join(', ')}.`
      : `stop ${PROGRAMS[program]} first.`
    return `${PROGRAMS[program]} holds ${listed(names)} in memory now. The models of this bench that run on ${listed(others)} would share `
      + `the memory of this machine with it: what does not fit the graphics card runs on the processor, which is slow. To have one at a time, ${free}`
  }).filter((line): line is string => line !== null)
  return lines.length ? lines.join(' ') : null
}

/** What the bench that is open is, in the line over its table. `made`: how many of its runs have a mark. The tools
 * are named once a run has been made: before the first one no model was offered any yet, and "0 tools offered" read
 * as a bench without tools. */
export function benchLine(run: BenchRun, made: number): string {
  const tools = made > 0 || run.tools_offered > 0 ? `, ${many(run.tools_offered, 'tool')} offered` : ''
  return `${stateWord(run.status)} · ${many(run.models.length, 'model')}, ${many(run.repeats, 'repeat')}${tools}`
}

export const stateWord = (status: BenchStatus): string =>
  ({ running: 'Running', done: 'Finished', cancelled: 'Cancelled', interrupted: 'Interrupted', stopped: 'Stopped' })[status]

export const verdictWord = (status: BenchMarkStatus): string => ({ pass: 'Pass', fail: 'Fail', error: 'Error', skipped: 'Skipped' })[status]

/** The mark of a run in its line: the owner's own with what the rules said beside it, else the one the rules gave. */
export const verdictLine = (r: Marked): string =>
  r.by_hand ? `${verdictWord(r.by_hand)} · set by hand (the rules said ${verdictWord(r.status)})` : verdictWord(r.status)

/** What the owner can do with the mark of a run: set the other one, or take his own back. null for a run the rules
 * gave no mark: an error and a skipped run have no answer to judge. */
export function handChoice(r: Marked): { label: string; status: 'pass' | 'fail' | null } | null {
  if (r.status !== 'pass' && r.status !== 'fail') return null
  if (r.by_hand) return { label: 'Undo', status: null }
  return r.status === 'fail' ? { label: 'Count as pass', status: 'pass' } : { label: 'Count as fail', status: 'fail' }
}
