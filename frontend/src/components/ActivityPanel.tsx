import { useEffect, useState } from 'react'
import * as api from '../api'
import { compact, visible } from '../tree'
import type { ActivitySummary } from '../types'
import { Modal } from './Modal'

const RANGES: [number, string][] = [[1, 'Today and yesterday'], [7, 'Last 7 days'], [30, 'Last 30 days'], [365, 'Last year']]

/** What HunterAI did: calls, tokens, durations and failures, counted on this machine and sent nowhere. */
export function ActivityPanel({ onClose }: { onClose: () => void }) {
  const [days, setDays] = useState(7)
  const [data, setData] = useState<ActivitySummary | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let stop = false
    setError(null)
    api.activity(days).then((d) => { if (!stop) setData(d) }).catch((e) => { if (!stop) setError(e instanceof Error ? e.message : String(e)) })
    return () => { stop = true }
  }, [days])
  const t = data?.totals
  return (
    <Modal wide title="Activity" onClose={onClose} footer={<>
      <span className="hint grow">Counted on this machine from the trajectories of your chats. Nothing is sent anywhere.</span>
      <button className="btn primary" onClick={onClose}>Done</button></>}>
      <div className="row">
        <select aria-label="Period" value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {RANGES.map(([n, label]) => <option key={n} value={n}>{label}</option>)}</select>
        {error && <span className="warn-line">{error}</span>}
      </div>
      {t && (<div className="activity-stats">
        <div className="stat"><div className="k">Runs</div><div className="v">{t.runs}</div></div>
        <div className="stat"><div className="k">Model calls</div><div className="v">{t.model_calls}</div></div>
        <div className="stat"><div className="k">Tool calls</div><div className="v">{t.tool_calls}</div></div>
        <div className="stat"><div className="k">Tokens in</div><div className="v" title={t.input_tokens.toLocaleString()}>{compact(t.input_tokens)}</div></div>
        <div className="stat"><div className="k">Tokens out</div><div className="v" title={t.output_tokens.toLocaleString()}>{compact(t.output_tokens)}</div></div>
        <div className="stat"><div className="k">Failed calls</div><div className={`v ${t.errors ? 'warn-line' : ''}`}>{t.errors}</div></div>
      </div>)}
      {data && t && t.runs === 0 && <div className="hint">Nothing ran in this period.</div>}
      {data && data.models.length > 0 && (<>
        <h3 className="section">Models</h3>
        <table className="activity-table"><thead><tr><th>Model</th><th>Calls</th><th>Failed</th><th>Tokens in</th><th>Tokens out</th><th>Tokens in per call</th><th>Average time</th></tr></thead>
          <tbody>{data.models.map((m) => (<tr key={m.name}><td className="mono">{visible(m.name)}</td><td>{m.calls}</td><td className={m.errors ? 'warn-line' : ''}>{m.errors}</td>
            <td title={m.input_tokens.toLocaleString()}>{compact(m.input_tokens)}</td><td>{compact(m.output_tokens)}</td>
            <td>{compact(Math.round(m.input_tokens / Math.max(1, m.calls - m.errors)))}</td><td>{m.avg_seconds} s</td></tr>))}</tbody></table>
      </>)}
      {data && data.tools.length > 0 && (<>
        <h3 className="section">Tools</h3>
        <table className="activity-table"><thead><tr><th>Tool</th><th>Calls</th><th>Failed or denied</th><th>Average time</th></tr></thead>
          <tbody>{data.tools.map((x) => (<tr key={x.name}><td className="mono">{visible(x.name)}</td><td>{x.calls}</td><td className={x.errors ? 'warn-line' : ''}>{x.errors}</td><td>{x.avg_seconds} s</td></tr>))}</tbody></table>
      </>)}
      {data && data.decisions.calls > 0 && <div className="hint">Decision model: {data.decisions.calls} calls (risk gate, verdicts, triage, answer checks), {data.decisions.errors} failed, {data.decisions.avg_seconds} s on average.</div>}
      {data && data.days.length > 0 && (<>
        <h3 className="section">By day</h3>
        <table className="activity-table"><thead><tr><th>Day</th><th>Runs</th><th>Model calls</th><th>Tool calls</th><th>Tokens in</th><th>Tokens out</th><th>Failed</th></tr></thead>
          <tbody>{data.days.map((d) => (<tr key={d.day}><td className="mono">{d.day}</td><td>{d.runs}</td><td>{d.model_calls}</td><td>{d.tool_calls}</td>
            <td>{compact(d.input_tokens)}</td><td>{compact(d.output_tokens)}</td><td className={d.errors ? 'warn-line' : ''}>{d.errors}</td></tr>))}</tbody></table>
      </>)}
      {data && <div className="hint">Deleting a chat removes its runs from these counts. The log file keeps one line per call for good, with names, times and token counts but no arguments or results: <span className="mono">{data.log_file}</span></div>}
    </Modal>
  )
}
