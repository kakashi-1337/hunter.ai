import { useRef, useState } from 'react'
import * as api from '../api'
import { proxyTestText } from '../proxy'
import { briefSummary, placeEntry, scopeCheckKey, scopeOf, scopeProblemText, scopeTone, withBrief, type ScopeForm } from '../scope'
import type { ScopeAnswer } from '../types'

export const SCOPE_HINT = 'Scope is given to the model as context and checked on every tool call: a host on the out-of-scope list, '
  + 'or one that is not on the in-scope list, always asks you first. A host you list by name stays in scope where a wildcard '
  + 'on the out-of-scope list covers it; a host on both lists is out.'

/** The fields of a target, and a box that reads a pasted program brief into them. The brief is read by rules on the
 * server; what it found lands in the fields for the owner to check, and nothing is saved from here. */
/** How the server starts what it says about a proxy address it will not keep. A dialog shows that beside the proxy
 * field, and drops it once the field holds another address: the refusal was about the one that was sent. */
export const PROXY_REFUSED = 'The proxy address is refused'

/** A target's own proxy: the address, and a button that sends one request through it and says what sites see.
 * `targetId` lets the server test an address shown without its password with the password it has stored. */
function ProxyField({ value, kept, targetId, problem, onChange }: {
  value: string; kept?: boolean; targetId?: string; problem?: string; onChange(value: string): void
}) {
  const [testing, setTesting] = useState(false)
  const [found, setFound] = useState('')
  const test = async () => {
    setTesting(true)
    setFound('')
    try {
      setFound(proxyTestText(await api.testProxy(value, targetId)))
    } catch (e) {
      setFound(e instanceof Error ? e.message : String(e))
    } finally {
      setTesting(false)
    }
  }
  return (
    <>
      <label className="field"><span>Proxy for this target (optional){kept ? ' · a password is stored and not shown' : ''}</span>
        <input className="mono" spellCheck={false} autoComplete="off" value={value} aria-label="Proxy for this target"
          placeholder="socks5://user:password@host:port or http://user:password@host:port"
          onChange={(e) => { setFound(''); onChange(e.target.value) }} />
      </label>
      {problem && <div className="warn-line scope-problem" role="alert">{problem}</div>}
      <div className="row">
        <button className="btn" disabled={!value.trim() || testing} onClick={() => void test()}>{testing ? 'Testing…' : 'Test the proxy'}</button>
        <span className="hint grow" role="status">{found || 'For a program that asks you to come through its proxy, or to reach a target that is blocked from where you are. Chats on this target use it for web_fetch, web_download, the browser and commands, instead of the proxy in Settings.'}</span>
      </div>
    </>
  )
}

const SCOPE_CHECK_HINT = 'Says which line of the two lists decides for it, as they stand here, saved or not. No name is looked up.'

/** One host, address or URL against the two lists as they stand in the form. The server answers with the verdict the
 * permission check gives and the line behind it. It only reads: the lists stay as they are and nothing is saved. */
function ScopeCheck({ form }: { form: ScopeForm }) {
  const [ask, setAsk] = useState('')
  const [checking, setChecking] = useState(false)
  const [got, setGot] = useState<{ key: string; answer?: ScopeAnswer; error?: string } | null>(null)
  const key = scopeCheckKey(form, ask)
  const check = async () => {
    if (!ask.trim() || checking) return
    setChecking(true)
    try {
      setGot({ key, answer: await api.checkScope(scopeOf(form), ask) })
    } catch (e) {
      setGot({ key, error: e instanceof Error ? e.message : String(e) })
    } finally {
      setChecking(false)
    }
  }
  const now = got?.key === key ? got : null       // the lists or the host were changed since: that answer was about something else
  return (
    <div className="scope-check">
      <label className="field"><span>Check a host against these lists</span>
        <input className="mono" spellCheck={false} autoComplete="off" value={ask} aria-label="Host, address or URL to check"
          placeholder="shop.example.com or https://shop.example.com/login" onChange={(e) => setAsk(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void check() }} />
      </label>
      <div className="row">
        <button className="btn" disabled={!ask.trim() || checking} onClick={() => void check()}>{checking ? 'Checking…' : 'Check'}</button>
        <span className="hint grow" role="status">{now?.error || SCOPE_CHECK_HINT}</span>
      </div>
      {now?.answer && (
        <div className={`scope-answer ${scopeTone(now.answer.verdict, now.answer.command?.verdict)}`} role="status">
          <div className={`says ${scopeTone(now.answer.verdict)}`}>{now.answer.says}</div>
          {now.answer.command && <div className={`says ${scopeTone(now.answer.command.verdict)}`}>{now.answer.command.says}</div>}
          {now.answer.hints.map((hint) => <div className="hint" key={hint}>{hint}</div>)}
          {now.answer.unreadable.length > 0 && <div className="warn-line scope-problem">{scopeProblemText(now.answer.unreadable)}</div>}
        </div>
      )}
    </div>
  )
}

export function TargetForm({ form, problem, proxy, proxyKept, targetId, onProxy, onChange, onUnread, onEnter, focusName }: {
  form: ScopeForm
  problem?: string                    // what is wrong with the form as it stands, in the words of the server
  proxy: string                       // this target's own proxy as typed, or as the server shows a saved one
  proxyKept?: boolean                 // a password is stored for the saved one
  targetId?: string
  onProxy(value: string): void
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
      {problem && !problem.startsWith(PROXY_REFUSED) && <div className="warn-line scope-problem" role="alert">{problem}</div>}
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
      <ScopeCheck form={form} />
      <ProxyField value={proxy} kept={proxyKept && Boolean(proxy.trim())} targetId={targetId} onChange={onProxy}
        problem={problem?.startsWith(PROXY_REFUSED) ? problem : undefined} />
    </>
  )
}
