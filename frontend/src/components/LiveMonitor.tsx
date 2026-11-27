import { useEffect, useState } from 'react'
import * as api from '../api'
import { meterGroups, type Meter } from '../monitor'
import type { MonitorReading } from '../types'

const EVERY_MS = 3_000

function MeterRow({ m }: { m: Meter }) {
  const filled = m.share === null ? null : m.share * 100
  return (
    <div className={`meter-row${m.level ? ` ${m.level}` : ''}`} title={m.about}>
      <div className="meter-head"><span>{m.label}</span><output>{m.figure}</output></div>
      {filled !== null && (
        <div className="meter" role="meter" aria-label={m.label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(filled)} aria-valuetext={m.figure}>
          <span style={{ width: `${filled.toFixed(1)}%` }} />
        </div>)}
      {m.parts.length > 0 && <ul className="meter-parts">{m.parts.map((p, i) => <li key={i}><span>{p.name}</span><span>{p.figure}</span></li>)}</ul>}
    </div>
  )
}

/** The machine as it is now: how busy the processor is, who holds the memory, and each graphics card's memory, load,
 * temperature and power. Read every three seconds while it is open and the page is in view, one request at a time:
 * a look at a card starts two programs on the server, and keeps a card awake that a laptop would put to sleep. */
export function LiveMonitor({ onClose }: { onClose: () => void }) {
  const [reading, setReading] = useState<MonitorReading | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let stop = false
    let asking = false
    const ask = () => {
      if (document.hidden || asking) return
      asking = true
      api.systemMonitor()
        .then((r) => { if (!stop) { setReading(r); setFailed(false) } })
        .catch(() => { if (!stop) setFailed(true) })
        .finally(() => { asking = false })
    }
    ask()
    const t = setInterval(ask, EVERY_MS)
    document.addEventListener('visibilitychange', ask)
    return () => { stop = true; clearInterval(t); document.removeEventListener('visibilitychange', ask) }
  }, [])
  return (
    <section className="monitor" aria-label="Live monitor">
      <header><b>Live monitor</b><button className="icon" aria-label="Close the live monitor" onClick={onClose}>✕</button></header>
      {failed && <div className="monitor-note" role="alert">{reading ? 'The server did not answer the last look: these figures are from before.' : 'The server does not answer.'}</div>}
      {!reading && !failed && <div className="monitor-note">Reading the machine…</div>}
      {reading && meterGroups(reading).map((g, i) => (
        <div className="monitor-group" key={i}>
          <div className="section">{g.heading}</div>
          {g.meters.map((m) => <MeterRow key={m.label} m={m} />)}
        </div>))}
      {reading && !reading.cards.length && <div className="monitor-note">No NVIDIA card answers on this machine (nvidia-smi).</div>}
      <div className="hint">Read every three seconds while this is open, which keeps a graphics card awake that a laptop would put to sleep. Close it when you do not need it.</div>
    </section>
  )
}
