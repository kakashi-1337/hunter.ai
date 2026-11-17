import { useEffect, useState } from 'react'
import * as api from '../api'
import { machineLine, restartNotice, stoppedNotice } from '../tree'
import type { SystemStatus } from '../types'

const EVERY_MS = 10_000

/** How hot the processor is and which local models are loaded, asked again every ten seconds while the page is in view.
 * Above it, when the server is not the code on disk, the notice to restart it; and the parts that were in use and do
 * not run now, with the way to the setup check, which says how to start each again. */
export function MachineStatus({ onSetup }: { onSetup?: () => void }) {
  const [status, setStatus] = useState<SystemStatus | null>(null)
  useEffect(() => {
    let stop = false
    const ask = () => { if (!document.hidden) api.systemStatus().then((s) => { if (!stop) setStatus(s) }).catch(() => { if (!stop) setStatus(null) }) }
    ask()
    const t = setInterval(ask, EVERY_MS)
    document.addEventListener('visibilitychange', ask)
    return () => { stop = true; clearInterval(t); document.removeEventListener('visibilitychange', ask) }
  }, [])
  if (!status) return <div className="machine hint">local · 127.0.0.1</div>
  const { text, level } = machineLine(status)
  const title = level === 'bad' ? `At or over the limit of ${status.cpu_limit}°C: look_at_image waits for the processor to cool before it loads its model.`
    : level === 'warn' ? `Close to the limit of ${status.cpu_limit}°C.` : 'The processor temperature and the local models that hold memory now.'
  const restart = restartNotice(status)
  const down = stoppedNotice(status)
  // The owner stopped it himself: the server stops naming these parts until they have run again.
  const hide = () => { api.hideStopped().then(({ stopped }) => setStatus((s) => s && { ...s, stopped })).catch(() => {}) }
  return (
    <>
      {restart && <div className="restart-notice" role="alert">{restart}</div>}
      {down && (
        <div className="restart-notice down-notice" role="alert">
          <span>{down}</span>
          {onSetup && <button className="btn small" title="What stopped, and how to start it again" onClick={onSetup}>Setup check</button>}
          <button className="icon" aria-label="Hide this notice" title="I know: do not say it again until it has run again. The setup check keeps showing it." onClick={hide}>✕</button>
        </div>)}
      <div className={`machine ${level}`} role="status" title={title}><span className={`dot ${level === 'bad' ? 'error' : level === 'warn' ? 'cancelled' : 'done'}`} />{text || 'local · 127.0.0.1'}</div>
    </>
  )
}
