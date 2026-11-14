import { useEffect, useState } from 'react'
import * as api from '../api'
import { useStore } from '../store'
import { triageFacts, visible } from '../tree'
import type { Finding, FindingStatus, Goal, Severity } from '../types'
import { confirmBox } from '../dialog'
import { Modal } from './Modal'

const SEVERITIES: Severity[] = ['info', 'low', 'medium', 'high', 'critical']
const STATUSES: FindingStatus[] = ['open', 'confirmed', 'false_positive', 'duplicate', 'reported', 'fixed']

/** The chat's goal record and the findings recorded against its target. All text is plain. */
export function GoalPanel({ onClose }: { onClose: () => void }) {
  const chat = useStore((s) => s.chat)
  const steps = useStore((s) => s.live?.steps.length ?? 0)
  const setError = useStore((s) => s.setError)
  const [goal, setGoal] = useState<Goal | null>(null)
  const [findings, setFindings] = useState<Finding[]>([])
  const fail = (e: unknown) => setError(e instanceof Error ? e.message : String(e))
  const chatId = chat?.id
  const targetId = chat?.target_id

  const reload = async () => {
    if (!chatId) return
    try {
      setGoal(await api.getGoal(chatId))
      setFindings(targetId ? await api.listFindings(targetId) : [])
    } catch (e) { fail(e) }
  }
  useEffect(() => { void reload() }, [chatId, targetId, steps]) // eslint-disable-line react-hooks/exhaustive-deps

  const patch = async (f: Finding, change: Parameters<typeof api.patchFinding>[1]) => {
    try {
      const next = await api.patchFinding(f.id, change)
      setFindings((list) => list.map((x) => (x.id === f.id ? next : x)))
    } catch (e) { fail(e) }
  }
  const remove = async (f: Finding) => {
    if (!(await confirmBox(`Delete the finding "${f.title}"?`))) return
    try { await api.deleteFinding(f.id); setFindings((list) => list.filter((x) => x.id !== f.id)) } catch (e) { fail(e) }
  }
  const clear = async () => {
    if (!chatId || !(await confirmBox('Clear this chat\'s goal record?'))) return
    try { await api.clearGoal(chatId); setGoal(null) } catch (e) { fail(e) }
  }

  return (
    <Modal title="Goal and findings" onClose={onClose} footer={<button className="btn primary" onClick={onClose}>Done</button>}>
      <h3 className="section">Goal record</h3>
      {!goal ? <div className="hint">None yet. The agent sets one with the focus tool when a task takes more than a step or two; you can also ask it to.</div> : (
        <div className="goal">
          <div className="goal-objective">{visible(goal.objective)}</div>
          {goal.criteria.length > 0 && (<><div className="k">Success criteria</div><ul>{goal.criteria.map((c, i) => <li key={i}>{visible(c)}</li>)}</ul></>)}
          {goal.open_items.length > 0 && (<><div className="k">Open items</div><ul>{goal.open_items.map((c, i) => <li key={i}>{visible(c)}</li>)}</ul></>)}
          <div className="row"><span className="hint grow">Updated {goal.updated_at}</span><button className="btn danger" onClick={clear}>Clear</button></div>
        </div>
      )}
      <h3 className="section">Findings{findings.length ? ` (${findings.length})` : ''}</h3>
      {!targetId && <div className="hint">Findings are kept per target. Pick a target for this chat to record and see them.</div>}
      {targetId && findings.length === 0 && <div className="hint">None recorded for this target yet.</div>}
      {findings.map((f) => (
        <div key={f.id} className="finding" data-severity={f.severity}>
          <div className="row">
            <span className={`badge sev-${f.severity}`}>{f.severity}</span>
            <b className="grow">{visible(f.title)}</b>
            <select aria-label="Severity" value={f.severity} onChange={(e) => void patch(f, { severity: e.target.value as Severity })}>
              {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            <select aria-label="Status" value={f.status} onChange={(e) => void patch(f, { status: e.target.value as FindingStatus })}>
              {STATUSES.map((s) => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}
            </select>
            <button className="icon" aria-label="Delete finding" title="Delete" onClick={() => void remove(f)}>✕</button>
          </div>
          {triageFacts(f.triage).length > 0 && <div className="hint">Model's triage (not a fact): {triageFacts(f.triage).join(' · ')}</div>}
          {f.evidence && <details><summary>Evidence</summary><pre>{visible(f.evidence)}</pre></details>}
        </div>
      ))}
    </Modal>
  )
}
