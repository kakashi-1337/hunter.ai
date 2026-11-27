import { useEffect, useState } from 'react'
import * as api from '../api'
import * as build from '../build'
import { heatNote, machineLine, reloadNotice, restartNotice, stoppedNotice } from '../tree'
import type { SystemStatus } from '../types'
import { LiveMonitor } from './LiveMonitor'

const EVERY_MS = 10_000

/** How hot the processor and the graphics card are and which local models are loaded, asked again every ten seconds
 * while the page is in view.
 * Above it, when the server is not the code on disk, the notice to restart it; when this page is older than the
 * interface that is built now, the notice to reload it; and the parts that were in use and do not run now, with the
 * way to the setup check, which says how to start each again. Beside the line, the button that opens the live monitor
 * above it: nothing is read for that one while it is closed. */
export function MachineStatus({ onSetup }: { onSetup?: () => void }) {
  const [status, setStatus] = useState<SystemStatus | null>(null)
  const [monitor, setMonitor] = useState(false)
  useEffect(() => {
    let stop = false
    const ask = () => { if (!document.hidden) api.systemStatus().then((s) => { if (!stop) setStatus(s) }).catch(() => { if (!stop) setStatus(null) }) }
    ask()
    const t = setInterval(ask, EVERY_MS)
    document.addEventListener('visibilitychange', ask)
    return () => { stop = true; clearInterval(t); document.removeEventListener('visibilitychange', ask) }
  }, [])
  if (!status) return <div className="machine hint">local · 127.0.0.1</div>
  const { text, level, about } = machineLine(status)
  const title = [heatNote(status), about].filter(Boolean).join(' ')
    || 'How hot the processor and the graphics card are, and the local models that hold memory now.'
  const restart = restartNotice(status)
  const stale = reloadNotice(status, build.ownScript())
  const down = stoppedNotice(status)
  // The owner stopped it himself: the server stops naming these parts until they have run again.
  const hide = () => { api.hideStopped().then(({ stopped }) => setStatus((s) => s && { ...s, stopped })).catch(() => {}) }
  return (
    <>
      {restart && <div className="restart-notice" role="alert">{restart}</div>}
      {stale && (
        <div className="restart-notice down-notice" role="alert">
          <span>{stale}</span>
          <button className="btn small" onClick={build.reload}>Reload</button>
        </div>)}
      {down && (
        <div className="restart-notice down-notice" role="alert">
          <span>{down}</span>
          {onSetup && <button className="btn small" title="What stopped, and how to start it again" onClick={onSetup}>Setup check</button>}
          <button className="icon" aria-label="Hide this notice" title="I know: do not say it again until it has run again. The setup check keeps showing it." onClick={hide}>✕</button>
        </div>)}
      {monitor && <LiveMonitor onClose={() => setMonitor(false)} />}
      <div className="machine-row">
        <div className={`machine ${level}`} role="status" title={title}><span className={`dot ${level === 'bad' ? 'error' : level === 'warn' ? 'cancelled' : 'done'}`} />{text || 'local · 127.0.0.1'}</div>
        <button className="btn small" aria-expanded={monitor} title="Live monitor: the processor, the memory and each graphics card as they are now"
          onClick={() => setMonitor((open) => !open)}>Monitor</button>
      </div>
    </>
  )
}
