import { useEffect, useRef, useState } from 'react'
import * as api from '../api'
import type { Checkup } from '../types'
import { copyText } from './Markdown'
import { Modal } from './Modal'

const OPTIONAL = (n: number) => `${n} optional ${n === 1 ? 'part is' : 'parts are'} missing: the feature named on that line stays off until it is there.`
const IN_THEM = 'versions and what runs and what does not, and no key, no proxy address, no target and no message'

/** What this install has and lacks, with the command or the place that fixes each line. Shown by itself on a first start.
 * A part that was in use and does not run now is marked apart from one that was never there. The state of the install
 * can be copied as text for a report. */
export function SetupChecklist({ onClose, onSettings }: { onClose: () => void; onSettings: () => void }) {
  const [check, setCheck] = useState<Checkup | null>(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [copy, setCopy] = useState<'idle' | 'busy' | 'copied'>('idle')
  const [copied, setCopied] = useState(false)             // once in this dialog: the line that says what was copied stays
  const [byHand, setByHand] = useState<string | null>(null)
  const told = useRef<HTMLDivElement>(null)
  const load = async () => {
    setBusy(true); setError(null)
    try { setCheck(await api.systemCheckup()) } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  useEffect(() => { void load() }, [])
  useEffect(() => { told.current?.scrollIntoView?.({ block: 'nearest' }) }, [copied, byHand])     // it is drawn under the list
  useEffect(() => {
    if (copy !== 'copied') return
    const t = setTimeout(() => setCopy('idle'), 2500)
    return () => clearTimeout(t)
  }, [copy])
  const copyDiagnostics = async () => {
    setCopy('busy'); setError(null); setByHand(null)
    try {
      const { text } = await api.systemDiagnostics()
      if (await copyText(text)) { setCopied(true); setCopy('copied') } else { setByHand(text); setCopy('idle') }
    } catch (e) {
      setError(`The diagnostics could not be had: ${e instanceof Error ? e.message : String(e)}`)
      setCopy('idle')
    }
  }
  const stopped = check?.items.filter((i) => i.stopped) ?? []
  const missing = check?.items.filter((i) => !i.ok && !i.stopped).length ?? 0
  const said: string[] = []
  if (check) {
    if (!check.ready) said.push('HunterAI has no model to talk to yet. Fix the first line; the others are optional and each switches one feature on.')
    if (stopped.length) said.push(`Not working: ${stopped.map((i) => i.label).join(', ')}. ${stopped.length === 1 ? 'Its line says how to start it again.' : 'Their lines say how to start them again.'}`)
    if (check.ready && missing) said.push(stopped.length ? OPTIONAL(missing) : `Ready to chat. ${OPTIONAL(missing)}`)
    if (check.ready && !missing && !stopped.length) said.push('Everything is in place.')
  }
  return (
    <Modal title="Setup check" onClose={onClose} footer={<>
      <button className="btn" disabled={busy} onClick={() => void load()}>{busy ? 'Checking…' : 'Check again'}</button>
      <button className="btn" disabled={copy === 'busy'} title={`The state of this install as text for a report: ${IN_THEM}.`}
        onClick={() => void copyDiagnostics()}>{copy === 'busy' ? 'Copying…' : copy === 'copied' ? 'Copied' : 'Copy diagnostics'}</button>
      <button className="btn" onClick={onSettings}>Open Settings</button>
      <button className="btn primary" onClick={onClose}>{check?.ready ? 'Done' : 'Close'}</button></>}>
      {error && <div className="ask-warning" role="alert">{error}</div>}
      {check && <div className={check.ready && !stopped.length ? 'hint' : 'ask-warning'} role="status">{said.join(' ')}</div>}
      {check?.items.map((i) => (
        <div key={i.id} className="setup-item">
          <div className="row">
            <span className={`dot ${i.ok ? 'done' : i.required || i.stopped ? 'error' : 'cancelled'}`} />
            <b className="grow">{i.label}</b>
            <span className={`badge ${i.ok ? 'ok' : i.required || i.stopped ? 'bad' : 'warn'}`}>{i.ok ? 'ready' : i.required ? 'needed' : i.stopped ? 'not working' : 'optional, missing'}</span>
          </div>
          <div className="hint">{i.detail}</div>
          {i.fix && <div className="setup-fix">{i.fix}</div>}
        </div>
      ))}
      <div className="hint">Everything runs on this machine: the app listens on 127.0.0.1 only, and your keys stay in the .env file.</div>
      <div ref={told}>
        {copied && byHand === null && <div className="hint">The diagnostics are copied: paste them where you report the problem. They hold {IN_THEM}.</div>}
        {byHand !== null && (<>
          <div className="hint">The browser did not let HunterAI copy the diagnostics: select the text below and copy it. It holds {IN_THEM}.</div>
          <textarea className="diagnostics" readOnly value={byHand} aria-label="Diagnostics" onFocus={(e) => e.currentTarget.select()} />
        </>)}
      </div>
    </Modal>
  )
}
