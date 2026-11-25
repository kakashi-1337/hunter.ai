import { useEffect, useState } from 'react'
import { useStore } from '../store'
import { checkStepLine, decisionSummary, formatDuration, retrievalLine, stepSummary, visible, windowStepLine } from '../tree'
import type { Step } from '../types'

const ms = (a: string, b: string | null, now: number) => Math.max(0, (b ? Date.parse(b) : now) - Date.parse(a))

function StepRow({ step, now }: { step: Step; now: number }) {
  const d = step.detail as Record<string, string | undefined>
  const resized = windowStepLine(step)
  return (
    <div className={`step${step.parent_step_id ? ' child' : ''}`} data-kind={step.kind}>
      <div className="top">
        <span className={`dot ${step.status}`} title={step.status} />
        <span className="kind">{step.kind.replace('_', ' ')}</span>
        <span className="name" title={step.name}>{step.name}</span>
        <span className="mono muted">{formatDuration(ms(step.started_at, step.ended_at, now))}</span>
      </div>
      {step.kind === 'tool_call' && (
        <div className="meta">
          <span className="mono">{visible(stepSummary(step))}</span> · {step.status}
          {typeof d.error === 'string' && <><br />{d.error}</>}
          {typeof d.result_preview === 'string' && (
            <details>
              <summary>Result preview</summary>
              <pre>{visible(d.result_preview)}</pre>
              {d.truncated && d.full_output_path && <div>truncated, full output at <span className="mono">{d.full_output_path}</span></div>}
            </details>
          )}
        </div>
      )}
      {step.kind === 'permission' && (
        <div className="meta">
          {stepSummary(step)}
          {(step.detail.warnings as string[] | undefined)?.map((w) => <div key={w} className="warn-line">⚠ {w}</div>)}
          {d.note ? <div>Your note: {d.note}</div> : null}
        </div>
      )}
      {step.kind === 'decision' && (
        <div className="meta">
          {visible(decisionSummary(step))}
          {typeof d.model === 'string' && <span className="muted"> · {d.provider}/{d.model}</span>}
        </div>
      )}
      {step.kind === 'retrieval' && (
        <div className="meta">
          <span className="mono">{visible(String(d.query ?? ''))}</span><br />
          {d.error ? <span className="warn-line">{visible(String(d.error))}</span> : <>{visible(retrievalLine(step.name, step.detail))}</>}
          {Array.isArray(step.detail.collections) && <> · {(step.detail.collections as string[]).join(', ')}</>}
        </div>
      )}
      {step.kind === 'user_note' && <div className="meta">Read by the agent here, with its next model call:<pre>{visible(String(d.text ?? ''))}</pre></div>}
      {step.kind === 'fallback' && <div className="meta">{d.from} → {d.to}<br />Reason: {d.reason}</div>}
      {step.kind === 'check' && <div className="meta">{checkStepLine(step)}</div>}
      {resized && <div className="meta">{visible(resized)}</div>}
      {step.kind === 'model_call' && (
        <div className="meta">
          {step.status}{step.input_tokens || step.output_tokens ? ` · ${step.input_tokens} in / ${step.output_tokens} out tokens` : ''}
          {d.error ? <><br />{d.error}</> : null}
        </div>
      )}
    </div>
  )
}

export function TrajectoryPanel({ onClose }: { onClose: () => void }) {
  const { runs, selectedRunId, runDetail, live } = useStore()
  const selectRun = useStore((s) => s.selectRun)
  const isLive = live !== null && live.runId === selectedRunId
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!isLive) return
    const t = setInterval(() => setNow(Date.now()), 200)
    return () => clearInterval(t)
  }, [isLive])

  const steps = isLive ? live.steps : runDetail?.steps ?? []
  const liveStart = isLive ? steps[0]?.started_at : undefined
  const totals = isLive
    ? {
        duration_ms: liveStart ? ms(liveStart, null, now) : 0,
        turns: steps.filter((s) => s.kind === 'model_call').length,
        calls: steps.filter((s) => s.kind === 'tool_call').length,
        input_tokens: steps.reduce((n, s) => n + s.input_tokens, 0),
        output_tokens: steps.reduce((n, s) => n + s.output_tokens, 0),
      }
    : runDetail?.totals
  const options = [...runs]
  if (isLive && !options.some((r) => r.id === live.runId)) {
    options.push({ id: live.runId, status: 'running' } as (typeof runs)[number])
  }

  return (
    <aside className="traj" aria-label="Agent trajectory">
      <div className="traj-head">
        <b>Trajectory</b>
        <select aria-label="Run" value={selectedRunId ?? ''} onChange={(e) => selectRun(e.target.value || null)} disabled={options.length === 0}>
          {options.length === 0 && <option value="">no runs yet</option>}
          {options.map((r, i) => <option key={r.id} value={r.id}>Run {i + 1} · {r.id === live?.runId ? 'running' : r.status}</option>)}
        </select>
        <button className="icon" aria-label="Close trajectory (Ctrl+J)" title="Close (Ctrl+J)" onClick={onClose}>✕</button>
      </div>
      <div className="stats">
        <div className="stat"><div className="k">Duration</div><div className="v" data-stat="duration">{totals ? formatDuration(totals.duration_ms) : '—'}</div></div>
        <div className="stat"><div className="k">Turns</div><div className="v" data-stat="turns">{totals?.turns ?? '—'}</div></div>
        <div className="stat"><div className="k">Calls</div><div className="v" data-stat="calls">{totals?.calls ?? '—'}</div></div>
      </div>
      <div className="steps">
        {totals && <div className="hint">Tokens: {totals.input_tokens} in / {totals.output_tokens} out{runDetail && !isLive && runDetail.error ? ` · ${runDetail.error}` : ''}</div>}
        {steps.length === 0 && <div className="empty">{selectedRunId ? 'No steps recorded yet.' : 'Send a message to see the run here.'}</div>}
        {steps.map((s) => <StepRow key={s.id} step={s} now={now} />)}
      </div>
    </aside>
  )
}
