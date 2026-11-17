import { useEffect, useState } from 'react'
import * as api from '../api'
import { machineLine, restartNotice } from '../tree'
import type { SystemStatus } from '../types'

const EVERY_MS = 10_000

/** How hot the processor is and which local models are loaded, asked again every ten seconds while the page is in view.
 * Above it, when the server is not the code on disk, the notice to restart it. */
export function MachineStatus() {
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
  return (
    <>
      {restart && <div className="restart-notice" role="alert">{restart}</div>}
      <div className={`machine ${level}`} role="status" title={title}><span className={`dot ${level === 'bad' ? 'error' : level === 'warn' ? 'cancelled' : 'done'}`} />{text || 'local · 127.0.0.1'}</div>
    </>
  )
}
