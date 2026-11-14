import { useEffect, useState } from 'react'
import * as api from '../api'
import type { Checkup } from '../types'
import { Modal } from './Modal'

/** What this install has and lacks, with the command or the place that fixes each line. Shown by itself on a first start. */
export function SetupChecklist({ onClose, onSettings }: { onClose: () => void; onSettings: () => void }) {
  const [check, setCheck] = useState<Checkup | null>(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const load = async () => {
    setBusy(true); setError(null)
    try { setCheck(await api.systemCheckup()) } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  useEffect(() => { void load() }, [])
  const missing = check?.items.filter((i) => !i.ok).length ?? 0
  return (
    <Modal title="Setup check" onClose={onClose} footer={<>
      <button className="btn" disabled={busy} onClick={() => void load()}>{busy ? 'Checking…' : 'Check again'}</button>
      <button className="btn" onClick={onSettings}>Open Settings</button>
      <button className="btn primary" onClick={onClose}>{check?.ready ? 'Done' : 'Close'}</button></>}>
      {error && <div className="ask-warning" role="alert">{error}</div>}
      {check && (<div className={check.ready ? 'hint' : 'ask-warning'} role="status">
        {!check.ready ? 'HunterAI has no model to talk to yet. Fix the first line; the others are optional and each switches one feature on.'
          : missing ? `Ready to chat. ${missing} optional ${missing === 1 ? 'part is' : 'parts are'} missing: the feature named on that line stays off until it is there.`
            : 'Everything is in place.'}</div>)}
      {check?.items.map((i) => (
        <div key={i.id} className="setup-item">
          <div className="row">
            <span className={`dot ${i.ok ? 'done' : i.required ? 'error' : 'cancelled'}`} />
            <b className="grow">{i.label}</b>
            <span className={`badge ${i.ok ? 'ok' : i.required ? 'bad' : 'warn'}`}>{i.ok ? 'ready' : i.required ? 'needed' : 'optional, missing'}</span>
          </div>
          <div className="hint">{i.detail}</div>
          {i.fix && <div className="setup-fix">{i.fix}</div>}
        </div>
      ))}
      <div className="hint">Everything runs on this machine: the app listens on 127.0.0.1 only, and your keys stay in the .env file.</div>
    </Modal>
  )
}
