import { useEffect, useState } from 'react'
import * as api from '../api'
import { caidoListLine, visible } from '../tree'
import type { CaidoExchange, CaidoRow, CaidoStatus } from '../types'
import { Modal } from './Modal'

const text = (e: unknown) => (e instanceof Error ? e.message : String(e))
const when = (at: CaidoRow['at']) => { const d = new Date(typeof at === 'string' && /^\d+$/.test(at) ? Number(at) : at ?? NaN); return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString() }

/** What Caido has recorded of the agent's traffic: the same history the `caido` tool reads. Everything shown is untrusted text. */
export function CaidoPanel({ onClose }: { onClose: () => void }) {
  const [status, setStatus] = useState<CaidoStatus | null>(null)
  const [rows, setRows] = useState<CaidoRow[]>([])
  const [total, setTotal] = useState(0)
  const [leftOut, setLeftOut] = useState(0)
  const [media, setMedia] = useState(false)
  const [filter, setFilter] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState<CaidoExchange | null>(null)

  const load = async (code = filter, withMedia = media) => {
    setBusy(true); setError(null)
    try {
      const s = await api.caidoStatus()
      setStatus(s)
      if (s.running) { const r = await api.caidoRequests(code, withMedia); setRows(r.rows); setTotal(r.total); setLeftOut(r.left_out ?? 0) } else { setRows([]); setTotal(0); setLeftOut(0) }
    } catch (e) { setError(text(e)) } finally { setBusy(false) }
  }
  useEffect(() => { void load() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const start = async () => {
    setBusy(true); setError(null)
    try { await api.caidoStart(); await load() } catch (e) { setError(text(e)); setBusy(false) }
  }
  const show = async (id: string) => {
    if (open?.id === id) { setOpen(null); return }
    try { setOpen(await api.caidoRequest(id)) } catch (e) { setError(text(e)) }
  }

  return (
    <Modal wide title="Caido" onClose={onClose} footer={<>
      {status?.running && status.url && <a className="btn" href={status.url} target="_blank" rel="noopener noreferrer">Open Caido</a>}
      <button className="btn" disabled={busy} onClick={() => void load()}>Refresh</button>
      <button className="btn primary" onClick={onClose}>Done</button></>}>
      {!status ? <div className="hint">{busy ? 'Asking Caido…' : ''}</div>
        : !status.available ? <div className="hint">Caido is not set up on this machine: put caido-cli on your PATH (or set HUNTERAI_CAIDO_BIN) and restart HunterAI. The caido tools stay hidden from the model until then.</div>
        : !status.running ? (<div className="row"><span className="grow">Caido is not running. HunterAI starts it the first time the agent browses or uses a caido tool.</span>
            <button className="btn" disabled={busy} onClick={() => void start()}>{busy ? 'Starting…' : 'Start it now'}</button></div>)
        : (<>
          <div className="row caido-facts">
            <span className="badge ok">running</span>
            {status.version && <span className="muted">Caido {status.version}</span>}
            <span className="muted">project {visible(status.project ?? '—')}</span>
            <span className="muted">{status.requests ?? 0} requests recorded</span>
            <span className="mono muted">{status.url}</span>
          </div>
          {status.temporary && <div className="hint">This project is temporary: its history is gone when Caido stops (a restart of the machine, for one).</div>}
          {status.error && <div className="warn-line">{visible(status.error)}</div>}
          <form className="row" onSubmit={(e) => { e.preventDefault(); void load() }}>
            <input className="grow mono" aria-label="HTTPQL filter" placeholder={'HTTPQL filter, for example  req.host.cont:"example.com" and resp.code.gte:400'} value={filter} onChange={(e) => setFilter(e.target.value)} />
            <button className="btn" disabled={busy}>Filter</button>
            {filter && <button type="button" className="btn" onClick={() => { setFilter(''); void load('') }}>Clear</button>}
            <label className="check" title="Images, fonts, audio and video are recorded too, and left out of this list unless ticked"><input type="checkbox" checked={media} onChange={(e) => { setMedia(e.target.checked); void load(filter, e.target.checked) }} /><span>Media too</span></label>
          </form>
          <div className="hint">{caidoListLine(rows.length, total, leftOut, { busy, filtered: Boolean(filter) })}</div>
          <div className="caido-rows">
            {rows.map((r) => (
              <div key={r.id} className="caido-row">
                <button className="caido-line" aria-expanded={open?.id === r.id} onClick={() => void show(r.id)}>
                  <span className="mono muted">{when(r.at)}</span>
                  <span className={`badge ${r.status === null ? 'warn' : r.status >= 500 ? 'bad' : r.status >= 400 ? 'warn' : ''}`}>{r.status ?? 'no answer'}</span>
                  <b className="mono">{visible(r.method)}</b>
                  <span className="mono grow caido-url" title={visible(r.url)}>{visible(r.url)}</span>
                  {r.length !== null && <span className="muted mono">{r.length.toLocaleString()} B</span>}
                  {r.ms !== null && <span className="muted mono">{r.ms} ms</span>}
                </button>
                {open?.id === r.id && (<div className="caido-open">
                  <div className="k">Request</div><pre>{visible(open.request)}</pre>
                  <div className="k">Response</div><pre>{open.response === null ? '(no response was recorded)' : visible(open.response)}</pre>
                </div>)}
              </div>
            ))}
          </div>
        </>)}
      {error && <div className="ask-warning" role="alert">{visible(error)}</div>}
    </Modal>
  )
}
