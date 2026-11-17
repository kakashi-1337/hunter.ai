import { useRef, useState } from 'react'
import * as api from '../api'
import { briefSummary, placeEntry, withBrief, type ScopeForm } from '../scope'

export const SCOPE_HINT = 'Scope is given to the model as context and checked on every tool call: a host on the out-of-scope list, '
  + 'or one that is not on the in-scope list, always asks you first. A host you list by name stays in scope where a wildcard '
  + 'on the out-of-scope list covers it; a host on both lists is out.'

/** The fields of a target, and a box that reads a pasted program brief into them. The brief is read by rules on the
 * server; what it found lands in the fields for the owner to check, and nothing is saved from here. */
export function TargetForm({ form, problem, onChange, onUnread, onEnter, focusName }: {
  form: ScopeForm
  problem?: string                    // what is wrong with the scope lists as they stand, in the words of the server
  onChange(update: (form: ScopeForm) => ScopeForm): void     // applied to the form as it is then: a brief can arrive late
  onUnread(unread: boolean): void     // the box holds text that was not read into the fields yet
  onEnter?(): void
  focusName?: boolean
}) {
  const [text, setText] = useState('')
  const [reading, setReading] = useState(false)
  const [found, setFound] = useState('')
  const [error, setError] = useState('')
  const pasted = useRef(false)
  const latest = useRef(0)

  const read = async (source: string) => {
    if (!source.trim()) return
    const mine = ++latest.current
    setReading(true)
    setError('')
    try {
      const brief = await api.readBrief(source)
      if (mine !== latest.current) return           // the text changed while this one was being read
      onChange((f) => withBrief(f, brief))
      setFound(briefSummary(brief))
      onUnread(false)
    } catch (e) {
      if (mine === latest.current) setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (mine === latest.current) setReading(false)
    }
  }
  const edit = (value: string) => {
    setText(value)
    setFound('')
    setError('')
    onUnread(Boolean(value.trim()))
    if (pasted.current) void read(value)             // a paste is read at once; typing waits for the button
    pasted.current = false
  }
  const set = (patch: Partial<ScopeForm>) => onChange((f) => ({ ...f, ...patch }))

  return (
    <>
      <label className="field"><span>Program brief or scope (optional)</span>
        <textarea rows={4} value={text} aria-label="Program brief or scope" onPaste={() => { pasted.current = true }} onChange={(e) => edit(e.target.value)}
          placeholder="Paste the program page from HackerOne, Bugcrowd, YesWeHack or Intigriti, a scope table, or a list of hosts" />
      </label>
      <div className="row">
        <button className="btn" disabled={!text.trim() || reading} onClick={() => void read(text)}>{reading ? 'Reading…' : 'Read the brief'}</button>
        <span className="hint grow" role="status">{error || found || 'Rules read it, not a model. What it finds goes into the fields below for you to check.'}</span>
      </div>
      <label className="field"><span>Name</span>
        <input autoFocus={focusName} value={form.name} onChange={(e) => set({ name: e.target.value })} placeholder="Program or client name"
          onKeyDown={(e) => { if (e.key === 'Enter' && onEnter) { e.preventDefault(); onEnter() } }} />
      </label>
      <label className="field"><span>Notes</span>
        <textarea rows={3} value={form.notes} onChange={(e) => set({ notes: e.target.value })} placeholder="Rules of engagement, accounts, anything the model should know" />
      </label>
      <label className="field"><span>In scope (one per line)</span>
        <textarea className="mono" rows={4} value={form.inScope} onChange={(e) => set({ inScope: e.target.value })} placeholder={'*.example.com\napi.example.com/v2'} />
      </label>
      <label className="field"><span>Out of scope (one per line)</span>
        <textarea className="mono" rows={3} value={form.outScope} onChange={(e) => set({ outScope: e.target.value })} placeholder="payments.example.com" />
      </label>
      {problem && <div className="warn-line scope-problem" role="alert">{problem}</div>}
      {form.unplaced.length > 0 && (
        <div className="unplaced" role="group" aria-label="Hosts with no side given">
          <div className="hint">The text names these without saying which side they are on. They are saved only if you place them.</div>
          {form.unplaced.map((entry) => (
            <div className="row" key={entry}>
              <span className="mono grow" title={entry}>{entry}</span>
              <button className="btn small" aria-label={`Put ${entry} in scope`} onClick={() => onChange((f) => placeEntry(f, entry, 'in'))}>In scope</button>
              <button className="btn small" aria-label={`Put ${entry} out of scope`} onClick={() => onChange((f) => placeEntry(f, entry, 'out'))}>Out of scope</button>
              <button className="icon" title="Leave it out" aria-label={`Leave ${entry} out`} onClick={() => onChange((f) => ({ ...f, unplaced: f.unplaced.filter((x) => x !== entry) }))}>✕</button>
            </div>
          ))}
        </div>
      )}
      {form.unsupported.length > 0 && (
        <div className="hint">No scope entry can say these; write the hosts they stand for by hand: <span className="mono">{form.unsupported.join(', ')}</span></div>
      )}
      <div className="hint">{SCOPE_HINT}</div>
    </>
  )
}
